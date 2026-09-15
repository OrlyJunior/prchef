from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orchestrator.clients.claude_code_client import ClaudeCodeClient
from orchestrator.clients.command_client import CommandResult
from orchestrator.clients.exceptions import CommandExecutionError


class RecordingCommandClient:
    def __init__(self, *, exit_code: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr

    def run(self, command: list[str], **kwargs: Any) -> CommandResult:
        self.calls.append((command, kwargs))
        return CommandResult(command, self.exit_code, self.stdout, self.stderr, 0.1)


def _success(**values: object) -> str:
    return json.dumps({"type": "result", "subtype": "success", "result": "done", **values})


def test_claude_code_client_builds_a_safe_print_mode_command_and_uses_workspace_stdin(
    tmp_path: Path,
) -> None:
    commands = RecordingCommandClient(stdout=_success(session_id="session-1", model="sonnet"))
    client = ClaudeCodeClient(
        commands,  # type: ignore[arg-type]
        ["claude"],
        30,
        model="sonnet",
        permission_mode="acceptEdits",
        max_turns=3,
        max_budget_usd=2.5,
    )

    result = client.execute("private prompt", tmp_path)

    command, kwargs = commands.calls[0]
    assert result.success
    assert result.session_id == "session-1"
    assert command == [
        "claude",
        "--print",
        "--output-format",
        "json",
        "--bare",
        "--permission-mode",
        "acceptEdits",
        "--permission-prompts",
        "none",
        "--model",
        "sonnet",
        "--max-turns",
        "3",
        "--max-budget-usd",
        "2.5",
    ]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["input_text"] == "private prompt"
    assert "private prompt" not in command
    assert "--add-dir" not in command
    assert "--dangerously-skip-permissions" not in command


def test_claude_code_client_extracts_native_structured_output_and_validates_schema(
    tmp_path: Path,
) -> None:
    commands = RecordingCommandClient(
        stdout=_success(
            result="human readable result",
            structured_output={"plan": "ready"},
            session_id="session-2",
            usage={"input_tokens": 12, "output_tokens": 3},
        )
    )
    schema = {
        "type": "object",
        "properties": {"plan": {"type": "string"}},
        "required": ["plan"],
    }

    result = ClaudeCodeClient(commands, ["claude"], 30).execute(
        "private prompt", tmp_path, output_schema=schema
    )

    command, _ = commands.calls[0]
    assert result.success
    assert result.output == "human readable result"
    assert result.structured_output == {"plan": "ready"}
    assert result.metadata is not None
    assert result.metadata["usage"] == {"input_tokens": 12, "output_tokens": 3}
    assert json.loads(command[command.index("--json-schema") + 1]) == schema


def test_claude_code_client_rejects_invalid_or_unsuccessful_envelopes(tmp_path: Path) -> None:
    malformed = ClaudeCodeClient(
        RecordingCommandClient(stdout="not json"),  # type: ignore[arg-type]
        ["claude"],
        30,
    ).execute("prompt", tmp_path)
    failed = ClaudeCodeClient(
        RecordingCommandClient(
            exit_code=1,
            stdout=json.dumps({"type": "result", "subtype": "error", "error": "rate_limit"}),
        ),  # type: ignore[arg-type]
        ["claude"],
        30,
    ).execute("prompt", tmp_path)
    invalid_structured = ClaudeCodeClient(
        RecordingCommandClient(
            stdout=_success(structured_output={"plan": 3})
        ),  # type: ignore[arg-type]
        ["claude"],
        30,
    ).execute(
        "prompt",
        tmp_path,
        output_schema={"type": "object", "properties": {"plan": {"type": "string"}}},
    )

    assert malformed.error_category == "malformed_output"
    assert malformed.workspace_changes_may_exist
    assert failed.error_category == "rate_limit"
    assert failed.workspace_changes_may_exist
    assert invalid_structured.error_category == "invalid_structured_output"


def test_claude_code_client_reports_missing_runtime_and_timeout_without_secrets(
    tmp_path: Path,
) -> None:
    class FailingCommandClient:
        def run(self, command: list[str], **kwargs: Any) -> CommandResult:
            del command, kwargs
            raise CommandExecutionError("Command timed out: ANTHROPIC_API_KEY=secret-value")

    missing_workspace = ClaudeCodeClient(
        RecordingCommandClient(),  # type: ignore[arg-type]
        ["claude"],
        30,
    ).execute("prompt", tmp_path / "missing")
    timed_out = ClaudeCodeClient(
        FailingCommandClient(), ["claude"], 30  # type: ignore[arg-type]
    ).execute("prompt", tmp_path)

    assert missing_workspace.error_category == "invalid_request"
    assert not missing_workspace.workspace_changes_may_exist
    assert timed_out.error_category == "timeout"
    assert timed_out.workspace_changes_may_exist
    assert "secret-value" not in (timed_out.error_message or "")


def test_claude_code_client_normalizes_runtime_and_cli_failure_categories(tmp_path: Path) -> None:
    class MissingExecutableCommandClient:
        def run(self, command: list[str], **kwargs: Any) -> CommandResult:
            del command, kwargs
            raise CommandExecutionError("Executable not found: claude")

    unavailable = ClaudeCodeClient(
        MissingExecutableCommandClient(), ["claude"], 30  # type: ignore[arg-type]
    ).execute("prompt", tmp_path)

    assert unavailable.error_category == "unavailable"
    assert unavailable.error_message is not None
    assert "executable not found" not in unavailable.error_message.casefold()

    for details, category in (
        ("authentication_failed", "authentication"),
        ("quota exhausted", "quota"),
        ("rate_limit", "rate_limit"),
        ("overloaded", "capacity"),
        ("model_not_found", "invalid_model"),
        ("an unexpected failure", "unknown"),
    ):
        result = ClaudeCodeClient(
            RecordingCommandClient(
                exit_code=1,
                stdout=json.dumps(
                    {"type": "result", "subtype": "error", "error": details}
                ),
            ),  # type: ignore[arg-type]
            ["claude"],
            30,
        ).execute("prompt", tmp_path)

        assert result.error_category == category
        assert details not in (result.error_message or "")


def test_claude_code_client_identifies_an_unsupported_cli_option(tmp_path: Path) -> None:
    result = ClaudeCodeClient(
        RecordingCommandClient(exit_code=1, stderr="unknown option --permission-prompts"),
        ["claude"],
        30,
    ).execute("prompt", tmp_path)

    assert result.error_category == "unsupported_runtime"
    assert "2.1.259" in (result.error_message or "")
