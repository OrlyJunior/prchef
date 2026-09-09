from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from orchestrator.clients import codex_client, command_client
from orchestrator.clients.codex_client import CodexClient
from orchestrator.clients.command_client import CommandClient, CommandResult
from orchestrator.clients.exceptions import CommandExecutionError
from orchestrator.config.project_config import CodexCliProviderSettings


def _completed(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, stdout="completed", stderr="")


def test_non_windows_command_is_passed_to_subprocess_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launched: list[list[str]] = []

    monkeypatch.setattr(command_client, "_is_windows", lambda: False)
    monkeypatch.setattr(
        command_client.shutil,
        "which",
        lambda *args, **kwargs: pytest.fail("non-Windows should not resolve shims"),
    )
    monkeypatch.setattr(
        command_client.subprocess,
        "run",
        lambda command, **kwargs: (launched.append(command), _completed(command))[1],
    )

    configured = ["tool", "argument with spaces", "&literal"]
    result = CommandClient().run(configured, cwd=tmp_path)

    assert launched == [configured]
    assert result.command == configured


@pytest.mark.parametrize("suffix", [".cmd", ".bat"])
def test_windows_native_shim_is_resolved_without_shell_parsing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, suffix: str
) -> None:
    launched: list[tuple[list[str], dict[str, Any]]] = []
    lookups: list[tuple[str, str | None]] = []

    monkeypatch.setattr(command_client, "_is_windows", lambda: True)

    def fake_which(candidate: str, *, path: str | None = None) -> str | None:
        lookups.append((candidate, path))
        if candidate == f"codex{suffix}":
            return f"C:\\tools\\codex{suffix}"
        return None

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        launched.append((command, kwargs))
        return _completed(command)

    monkeypatch.setattr(command_client.shutil, "which", fake_which)
    monkeypatch.setattr(command_client.subprocess, "run", fake_run)

    configured = ["codex", "exec", "argument with spaces", "&literal"]
    result = CommandClient().run(
        configured,
        cwd=tmp_path,
        env={"Path": "C:\\tools", "PATHEXT": ".CMD;.BAT"},
    )

    assert launched == [
        (
            [f"C:\\tools\\codex{suffix}", "exec", "argument with spaces", "&literal"],
            {
                "cwd": tmp_path,
                "input": None,
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": 900,
                "check": False,
                "env": {"Path": "C:\\tools", "PATHEXT": ".CMD;.BAT"},
                "shell": False,
            },
        )
    ]
    assert lookups[-1][0] == f"codex{suffix}"
    assert str(tmp_path) in (lookups[-1][1] or "")
    assert result.command == configured


@pytest.mark.skipif(os.name != "nt", reason="Windows command shims are Windows-specific")
@pytest.mark.parametrize("suffix", [".cmd", ".bat"])
def test_windows_native_shim_can_run_from_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, suffix: str
) -> None:
    command_name = f"orchestrator-test-shim-{suffix.removeprefix('.')}"
    shim = tmp_path / f"{command_name}{suffix}"
    shim.write_text("@echo off\r\necho %~1\r\n", encoding="utf-8")
    monkeypatch.setenv("PATH", os.pathsep.join((str(tmp_path), os.environ.get("PATH", ""))))

    result = CommandClient().run(
        [command_name, "argument with spaces"], cwd=tmp_path, timeout_seconds=10
    )

    assert result.success
    assert result.stdout.strip() == "argument with spaces"


def test_launch_diagnostics_log_resolved_command_without_stdin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    target_logger = logging.getLogger(command_client.__name__)
    previous_propagation = target_logger.propagate
    previous_disabled = target_logger.disabled
    target_logger.addHandler(caplog.handler)
    target_logger.propagate = False
    target_logger.disabled = False
    try:
        monkeypatch.setattr(command_client, "_is_windows", lambda: True)
        monkeypatch.setattr(
            command_client.shutil,
            "which",
            lambda candidate, path=None: "C:\\npm\\codex.cmd",
        )
        monkeypatch.setattr(
            command_client.subprocess,
            "run",
            lambda command, **kwargs: _completed(command),
        )

        with caplog.at_level(logging.DEBUG, logger=target_logger.name):
            CommandClient().run(
                ["codex", "exec", "--prompt", "prompt-argument", "argument with spaces"],
                cwd=tmp_path,
                input_text="private stdin prompt",
            )
    finally:
        target_logger.removeHandler(caplog.handler)
        target_logger.propagate = previous_propagation
        target_logger.disabled = previous_disabled

    messages = [record.getMessage() for record in caplog.records]
    assert "[command] Resolved executable: C:\\npm\\codex.cmd" in messages
    assert f"[command] Working directory: {tmp_path.resolve()}" in messages
    assert (
        "[command] Arguments: exec --prompt [REDACTED] argument with spaces" in messages
    )
    assert "private stdin prompt" not in caplog.text


def test_file_not_found_includes_safe_os_error_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    target_logger = logging.getLogger(command_client.__name__)
    previous_propagation = target_logger.propagate
    previous_disabled = target_logger.disabled
    target_logger.addHandler(caplog.handler)
    target_logger.propagate = False
    target_logger.disabled = False
    try:
        error = FileNotFoundError(2, "No such file", "missing-tool")
        error.winerror = 2  # type: ignore[attr-defined]

        def missing(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise error

        monkeypatch.setattr(command_client.subprocess, "run", missing)

        with caplog.at_level(logging.INFO, logger=target_logger.name):
            with pytest.raises(CommandExecutionError, match="Executable not found: missing-tool"):
                CommandClient().run(["missing-tool"], cwd=tmp_path)
    finally:
        target_logger.removeHandler(caplog.handler)
        target_logger.propagate = previous_propagation
        target_logger.disabled = previous_disabled

    assert "filename='missing-tool'" in caplog.text
    assert "errno=2" in caplog.text
    assert "winerror=2" in caplog.text


def test_missing_executable_keeps_existing_error_semantics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launched: list[list[str]] = []

    monkeypatch.setattr(command_client, "_is_windows", lambda: True)
    monkeypatch.setattr(command_client.shutil, "which", lambda *args, **kwargs: None)

    def missing(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        launched.append(command)
        raise FileNotFoundError

    monkeypatch.setattr(command_client.subprocess, "run", missing)

    with pytest.raises(CommandExecutionError, match="Executable not found: missing-tool"):
        CommandClient().run(["missing-tool", "argument with spaces"], cwd=tmp_path)

    assert launched == [["missing-tool", "argument with spaces"]]


class RecordingCommandClient:
    def __init__(
        self, *, exit_code: int = 0, stdout: str = "completed", stderr: str = ""
    ) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr

    def run(self, command: list[str], **kwargs: Any) -> CommandResult:
        self.calls.append((command, kwargs))
        return CommandResult(
            command=command,
            exit_code=self._exit_code,
            stdout=self._stdout,
            stderr=self._stderr,
            duration_seconds=0.1,
        )


def test_codex_client_delegates_its_configured_command_to_command_client(tmp_path: Path) -> None:
    commands = RecordingCommandClient()
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--sandbox", "workspace-write", "{output_file}", "-"],
        timeout_seconds=30,
    )

    client.implement("private prompt", tmp_path)

    command, kwargs = commands.calls[0]
    assert command[:5] == ["codex", "exec", "--json", "--sandbox", "workspace-write"]
    assert command[-1] == "-"
    assert "--approve-for-me" not in command
    assert "--output-schema" not in command
    assert kwargs["input_text"] == "private prompt"


def test_codex_client_materializes_and_cleans_up_output_schema(tmp_path: Path) -> None:
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
    }
    schema_paths: list[Path] = []

    class SchemaRecordingCommandClient(RecordingCommandClient):
        def run(self, command: list[str], **kwargs: Any) -> CommandResult:
            schema_path = Path(command[command.index("--output-schema") + 1])
            schema_paths.append(schema_path)
            assert schema_path.is_relative_to(tmp_path) is False
            assert json.loads(schema_path.read_text(encoding="utf-8")) == schema
            return super().run(command, **kwargs)

    commands = SchemaRecordingCommandClient()
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--output-last-message", "{output_file}", "-"],
        timeout_seconds=30,
    )

    client.implement("private prompt", tmp_path, output_schema=schema)

    command, _ = commands.calls[0]
    assert "--json" in command
    assert "--output-schema" in command
    assert len(schema_paths) == 1
    assert not schema_paths[0].exists()


def test_codex_client_keeps_jsonl_parsing_with_output_schema(tmp_path: Path) -> None:
    commands = RecordingCommandClient(
        stdout="\n".join(
            (
                '{"type":"item.completed","item":{"type":"file_change"}}',
                '{"type":"item.completed","item":{"type":"agent_message",'
                '"text":"{\\"plan\\": \\"ready\\"}"}}',
                '{"type":"turn.completed"}',
            )
        )
    )
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--output-last-message", "{output_file}", "-"],
        timeout_seconds=30,
    )

    result = client.implement(
        "Return the plan as JSON",
        tmp_path,
        output_schema={
            "type": "object",
            "properties": {"plan": {"type": "string"}},
        },
    )

    command, _ = commands.calls[0]
    assert "--json" in command
    assert "--output-schema" in command
    assert result.success
    assert result.summary == '{"plan": "ready"}'


def test_codex_client_cleans_up_output_schema_after_execution_error(tmp_path: Path) -> None:
    schema_path: Path | None = None

    class FailingCommandClient:
        def run(self, command: list[str], **kwargs: Any) -> CommandResult:
            nonlocal schema_path
            schema_path = Path(command[command.index("--output-schema") + 1])
            assert schema_path.exists()
            raise RuntimeError("Codex did not start")

    client = CodexClient(
        FailingCommandClient(),  # type: ignore[arg-type]
        ["codex", "exec", "--output-last-message", "{output_file}", "-"],
        timeout_seconds=30,
    )

    with pytest.raises(RuntimeError, match="Codex did not start"):
        client.implement("private prompt", tmp_path, output_schema={"type": "object"})

    assert schema_path is not None
    assert not schema_path.exists()


def test_codex_client_approve_for_me_replaces_explicit_sandbox(tmp_path: Path) -> None:
    commands = RecordingCommandClient()
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        [
            "codex",
            "exec",
            "--sandbox",
            "workspace-write",
            "--output-last-message",
            "{output_file}",
            "-",
        ],
        timeout_seconds=30,
        approval_policy="approve-for-me",
    )

    client.implement("private prompt", tmp_path)

    command, kwargs = commands.calls[0]
    assert command[:3] == ["codex", "exec", "--approve-for-me"]
    assert "--sandbox" not in command
    assert "--output-last-message" in command
    assert "danger-full-access" not in command
    assert command[-1] == "-"
    assert kwargs["input_text"] == "private prompt"


def test_codex_client_passes_configured_model_and_reasoning_effort(tmp_path: Path) -> None:
    commands = RecordingCommandClient()
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--sandbox", "workspace-write", "{output_file}", "-"],
        timeout_seconds=30,
        model="model-one",
        reasoning_effort="high",
    )

    client.implement("private prompt", tmp_path)

    command, _ = commands.calls[0]
    assert command[2:7] == [
        "--json",
        "--model",
        "model-one",
        "--config",
        'model_reasoning_effort="high"',
    ]
    assert command.count("--model") == 1


@pytest.mark.parametrize("model", ["model-one", "model-two"])
def test_codex_client_changes_only_the_selected_model_argument(
    tmp_path: Path, model: str
) -> None:
    commands = RecordingCommandClient(exit_code=1, stderr="configured model unavailable")
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "{output_file}", "-"],
        timeout_seconds=30,
        model=model,
    )

    result = client.implement("private prompt", tmp_path)

    assert not result.success
    assert commands.calls[0][0][3:5] == ["--model", model]
    assert len(commands.calls) == 1


def test_codex_client_preserves_custom_matching_runtime_flags(tmp_path: Path) -> None:
    commands = RecordingCommandClient()
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        [
            "codex",
            "exec",
            "--model",
            "model-one",
            "--config=model_reasoning_effort='high'",
            "{output_file}",
            "-",
        ],
        timeout_seconds=30,
        model="model-one",
        reasoning_effort="high",
    )

    client.implement("private prompt", tmp_path)

    command, _ = commands.calls[0]
    assert command.count("--model") == 1
    assert command.count("--config=model_reasoning_effort='high'") == 1


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (
            {"model": "model-one", "command": ["codex", "exec", "--model", "model-two"]},
            "conflicting model flag",
        ),
        (
            {
                "reasoning_effort": "high",
                "command": ["codex", "exec", "-c", "model_reasoning_effort=low"],
            },
            "conflicting model_reasoning_effort",
        ),
        (
            {"model": "model-one", "command": ["codex", "exec", "--model"]},
            "model flag without a value",
        ),
        (
            {
                "reasoning_effort": "high",
                "command": ["codex", "exec", "--config", "model_reasoning_effort="],
            },
            "model_reasoning_effort without a value",
        ),
    ],
)
def test_codex_custom_command_rejects_conflicting_runtime_settings(
    settings: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        CodexCliProviderSettings.model_validate({"type": "codex-cli", **settings})


@pytest.mark.parametrize("field", ["model", "reasoning_effort"])
def test_codex_empty_runtime_settings_fail_clearly(field: str) -> None:
    with pytest.raises(ValueError, match=f"{field} must be a non-empty string"):
        CodexCliProviderSettings.model_validate(
            {"type": "codex-cli", "command": ["codex", "exec", "-"], field: "  "}
        )


def test_codex_client_approve_for_me_preserves_model_and_reasoning(tmp_path: Path) -> None:
    commands = RecordingCommandClient()
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--sandbox", "workspace-write", "{output_file}", "-"],
        timeout_seconds=30,
        approval_policy="approve-for-me",
        model="model-one",
        reasoning_effort="medium",
    )

    client.implement("private prompt", tmp_path)

    command, _ = commands.calls[0]
    assert command[:8] == [
        "codex",
        "exec",
        "--approve-for-me",
        "--json",
        "--model",
        "model-one",
        "--config",
        'model_reasoning_effort="medium"',
    ]
    assert "--sandbox" not in command


def test_codex_runtime_settings_do_not_embed_a_hard_coded_model_or_leak_to_services() -> None:
    client_source = Path(codex_client.__file__).read_text(encoding="utf-8")
    services = Path(__file__).parents[2] / "src" / "orchestrator" / "services"

    assert "gpt-" not in client_source.casefold()
    for filename in ("spec_service.py", "implementation_service.py", "review_service.py"):
        source = (services / filename).read_text(encoding="utf-8")
        assert "reasoning_effort" not in source


def test_codex_client_ignores_sandbox_fixture_text_in_free_form_output(tmp_path: Path) -> None:
    commands = RecordingCommandClient(
        stdout="Creation failed because sandbox access to the requested file path was denied."
    )
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--sandbox", "workspace-write", "{output_file}", "-"],
        timeout_seconds=30,
    )

    result = client.implement("private prompt", tmp_path)

    assert result.success
    assert result.summary == (
        "Creation failed because sandbox access to the requested file path was denied."
    )
    assert result.error_message is None
    assert "danger-full-access" not in commands.calls[0][0]


def test_codex_client_ignores_policy_denied_text_in_successful_command_output(
    tmp_path: Path,
) -> None:
    stdout = "\n".join(
        (
            '{"type":"item.completed","item":{"type":"command_execution",'
            '"status":"completed","stdout":"codex_sandboxing policy_denied: '
            'Failed to write file output.txt"}}',
            '{"type":"item.completed","item":{"type":"file_change","status":"completed"}}',
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"Implementation completed."}}',
            '{"type":"turn.completed"}',
        )
    )
    commands = RecordingCommandClient(stdout=stdout)
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--sandbox", "workspace-write", "{output_file}", "-"],
        timeout_seconds=30,
    )

    result = client.implement("private prompt", tmp_path)

    assert result.success
    assert result.summary == "Implementation completed."
    assert result.error_message is None
    assert not result.workspace_changes_may_exist


def test_codex_client_surfaces_structured_sandbox_denial(tmp_path: Path) -> None:
    stdout = (
        '{"type":"item.failed","item":{"type":"command_execution",'
        '"status":"failed","error":{"code":"policy_denied",'
        '"message":"write access was denied","operation":"file-write-create",'
        '"path":"C:/restricted/output.txt"}}}'
    )
    commands = RecordingCommandClient(stdout=stdout)
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--sandbox", "workspace-write", "{output_file}", "-"],
        timeout_seconds=30,
    )

    result = client.implement("private prompt", tmp_path)

    assert not result.success
    assert result.workspace_changes_may_exist
    assert result.error_message is not None
    assert "structured sandbox denial" in result.error_message
    assert "operation=file-write-create" in result.error_message
    assert "path=C:/restricted/output.txt" in result.error_message
    assert "workspace write denial" not in result.error_message
    assert "exit 0" in result.error_message


def test_codex_client_surfaces_structured_sandbox_denial_for_command(
    tmp_path: Path,
) -> None:
    stdout = (
        '{"type":"turn.failed","error":{"code":"policy_denied",'
        '"details":{"operation":"process-exec","command":["git", "push", "origin", "main"]}}}'
    )
    commands = RecordingCommandClient(stdout=stdout)
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--sandbox", "workspace-write", "{output_file}", "-"],
        timeout_seconds=30,
    )

    result = client.implement("private prompt", tmp_path)

    assert not result.success
    assert result.error_message is not None
    assert (
        "sandbox denial for operation=process-exec, command=git push origin main"
        in result.error_message
    )
    assert "workspace write denial" not in result.error_message


def test_codex_client_uses_workspace_message_for_general_structured_sandbox_denial(
    tmp_path: Path,
) -> None:
    commands = RecordingCommandClient(
        stdout='{"type":"error","error":{"code":"policy_denied","message":"write denied"}}'
    )
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--sandbox", "workspace-write", "{output_file}", "-"],
        timeout_seconds=30,
    )

    result = client.implement("private prompt", tmp_path)

    assert not result.success
    assert result.error_message is not None
    assert "structured workspace write denial" in result.error_message


def test_codex_client_surfaces_structured_terminal_failure(tmp_path: Path) -> None:
    commands = RecordingCommandClient(
        stdout=(
            '{"type":"turn.failed","error":{"code":"execution_error",'
            '"message":"The requested implementation could not be completed."}}'
        )
    )
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--sandbox", "workspace-write", "{output_file}", "-"],
        timeout_seconds=30,
    )

    result = client.implement("private prompt", tmp_path)

    assert not result.success
    assert result.workspace_changes_may_exist
    assert result.error_message is not None
    assert "structured execution failure" in result.error_message
    assert "execution_error" in result.error_message


def test_codex_client_does_not_add_json_twice(tmp_path: Path) -> None:
    commands = RecordingCommandClient()
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--json", "{output_file}", "-"],
        timeout_seconds=30,
    )

    client.implement("private prompt", tmp_path)

    assert commands.calls[0][0].count("--json") == 1


def test_codex_client_preserves_non_sandbox_execution_errors(tmp_path: Path) -> None:
    commands = RecordingCommandClient(exit_code=1, stderr="permission denied: output.txt")
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--sandbox", "workspace-write", "{output_file}", "-"],
        timeout_seconds=30,
    )

    result = client.implement("private prompt", tmp_path)

    assert not result.success
    assert not result.workspace_changes_may_exist
    assert result.error_message == "permission denied: output.txt"
