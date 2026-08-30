from __future__ import annotations

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
    assert command[:4] == ["codex", "exec", "--sandbox", "workspace-write"]
    assert command[-1] == "-"
    assert kwargs["input_text"] == "private prompt"


def test_codex_client_surfaces_windows_sandbox_write_denial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(codex_client, "_is_windows", lambda: True)
    commands = RecordingCommandClient(
        stdout="Creation failed because sandbox access to the requested file path was denied."
    )
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--sandbox", "workspace-write", "{output_file}", "-"],
        timeout_seconds=30,
    )

    result = client.implement("private prompt", tmp_path)

    assert not result.success
    assert result.summary == (
        "Creation failed because sandbox access to the requested file path was denied."
    )
    assert result.error_message is not None
    assert "Codex Windows sandbox could not modify workspace files" in result.error_message
    assert f"repository: {tmp_path.resolve()}" in result.error_message
    assert "sandbox: workspace-write" in result.error_message
    assert "codex exec --sandbox workspace-write -" in result.error_message
    assert "danger-full-access" not in commands.calls[0][0]


def test_codex_client_surfaces_non_windows_sandbox_write_denial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(codex_client, "_is_windows", lambda: False)
    commands = RecordingCommandClient(
        stdout="Creation failed because sandbox access to the requested file path was denied."
    )
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--sandbox", "workspace-write", "{output_file}", "-"],
        timeout_seconds=30,
    )

    result = client.implement("private prompt", tmp_path)

    assert not result.success
    assert result.error_message is not None
    assert "Codex sandbox could not modify workspace files" in result.error_message
    assert "Windows" not in result.error_message


def test_codex_client_recognizes_policy_denied_sandbox_write_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(codex_client, "_is_windows", lambda: True)
    commands = RecordingCommandClient(
        stdout="codex_sandboxing policy_denied: Failed to write file output.txt"
    )
    client = CodexClient(
        commands,  # type: ignore[arg-type]
        ["codex", "exec", "--sandbox", "workspace-write", "{output_file}", "-"],
        timeout_seconds=30,
    )

    result = client.implement("private prompt", tmp_path)

    assert not result.success
    assert result.error_message is not None
    assert "Codex Windows sandbox" in result.error_message
