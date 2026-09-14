from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orchestrator.clients.claude_code_client import ClaudeCodeClient
from orchestrator.clients.command_client import CommandResult


class RecordingCommandClient:
    def __init__(self, *, stdout: str, exit_code: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.exit_code = exit_code
        self.stderr = stderr
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(self, command: list[str], **kwargs: Any) -> CommandResult:
        self.calls.append((command, kwargs))
        return CommandResult(
            command=command,
            exit_code=self.exit_code,
            stdout=self.stdout,
            stderr=self.stderr,
            duration_seconds=0.1,
        )


def test_claude_code_client_uses_non_interactive_json_mode_in_the_workspace(
    tmp_path: Path,
) -> None:
    commands = RecordingCommandClient(stdout='{"result":"done","session_id":"session-1"}')
    client = ClaudeCodeClient(
        commands,  # type: ignore[arg-type]
        "claude-custom",
        timeout_seconds=30,
        model="claude-sonnet-test",
        max_turns=4,
    )

    result = client.execute("private prompt", tmp_path)

    command, kwargs = commands.calls[0]
    assert command == [
        "claude-custom",
        "--print",
        "--output-format",
        "json",
        "--permission-mode",
        "acceptEdits",
        "--model",
        "claude-sonnet-test",
        "--max-turns",
        "4",
    ]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["input_text"] == "private prompt"
    assert kwargs["timeout_seconds"] == 30
    assert result.success
    assert result.output == "done"
    assert result.session_id == "session-1"


def test_claude_code_client_passes_native_json_schema_and_extracts_result(
    tmp_path: Path,
) -> None:
    schema = {
        "type": "object",
        "properties": {"plan": {"type": "string"}},
        "required": ["plan"],
    }
    commands = RecordingCommandClient(
        stdout=json.dumps(
            {
                "result": "A plan was produced.",
                "structured_output": {"plan": "ready"},
            }
        )
    )
    client = ClaudeCodeClient(commands)  # type: ignore[arg-type]

    result = client.execute("Return JSON", tmp_path, output_schema=schema)

    command, _ = commands.calls[0]
    assert command[command.index("--json-schema") + 1] == json.dumps(
        schema, ensure_ascii=False
    )
    assert result.structured_output == {"plan": "ready"}


def test_claude_code_client_reports_non_zero_exit_and_possible_workspace_changes(
    tmp_path: Path,
) -> None:
    commands = RecordingCommandClient(stdout="", stderr="request failed", exit_code=1)

    result = ClaudeCodeClient(commands).execute("implement", tmp_path)  # type: ignore[arg-type]

    assert not result.success
    assert result.error_message == "request failed"
    assert result.workspace_changes_may_exist


def test_claude_code_client_reports_malformed_json(tmp_path: Path) -> None:
    commands = RecordingCommandClient(stdout="not JSON")

    result = ClaudeCodeClient(commands).execute("implement", tmp_path)  # type: ignore[arg-type]

    assert not result.success
    assert result.error_message == "Claude Code returned malformed JSON output"
