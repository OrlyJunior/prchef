from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from orchestrator.clients.claude_code_client import ClaudeCodeClient
from orchestrator.clients.command_client import CommandResult
from orchestrator.clients.exceptions import CommandExecutionError
from orchestrator.config.project_config import ClaudeCodeProviderSettings, ProjectConfig
from orchestrator.models import AgentRequest
from orchestrator.providers.agent_provider import AgentProviderError
from orchestrator.providers.claude_code_provider import ClaudeCodeAgentProvider


class RecordingCommandClient:
    def __init__(self, *, exit_code: int = 0, stdout: str = "{}", stderr: str = "") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(self, command: list[str], **kwargs: Any) -> CommandResult:
        self.calls.append((command, kwargs))
        return CommandResult(command, self.exit_code, self.stdout, self.stderr, 0.1)


def _envelope(**values: Any) -> str:
    return json.dumps({"type": "result", "subtype": "success", **values})


def test_claude_code_client_uses_only_safe_print_mode_arguments_and_workspace(
    tmp_path: Path,
) -> None:
    commands = RecordingCommandClient(stdout=_envelope(result="done"))
    client = ClaudeCodeClient(
        commands,  # type: ignore[arg-type]
        "claude",
        30,
        model="claude-test",
        permission_mode="acceptEdits",
        max_turns=4,
        max_budget_usd=2.5,
    )

    result = client.execute("private prompt", tmp_path)

    command, kwargs = commands.calls[0]
    assert result.success
    assert command == [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        "acceptEdits",
        "--permission-prompts",
        "none",
        "--bare",
        "--model",
        "claude-test",
        "--max-turns",
        "4",
        "--max-budget-usd",
        "2.5",
    ]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["input_text"] == "private prompt"
    assert not any(
        flag in command
        for flag in ("--worktree", "--add-dir", "--resume", "--continue", "--cloud")
    )


def test_claude_code_client_forwards_native_schema_and_validates_result(tmp_path: Path) -> None:
    schema = {
        "type": "object",
        "properties": {"plan": {"type": "string"}},
        "required": ["plan"],
        "additionalProperties": False,
    }
    commands = RecordingCommandClient(
        stdout=_envelope(
            result="Plan is ready",
            structured_output={"plan": "ready"},
            session_id="session-123",
            num_turns=2,
            total_cost_usd=0.01,
        )
    )
    client = ClaudeCodeClient(commands, "claude", 30)  # type: ignore[arg-type]

    result = client.execute("Return a plan", tmp_path, schema)

    command, _ = commands.calls[0]
    assert json.loads(command[command.index("--json-schema") + 1]) == schema
    assert result.success
    assert result.summary == "Plan is ready"
    assert result.structured_output == {"plan": "ready"}
    assert result.session_id == "session-123"
    assert result.metadata == {"num_turns": 2, "total_cost_usd": 0.01, "subtype": "success"}


def test_claude_code_client_rejects_malformed_or_invalid_structured_output(tmp_path: Path) -> None:
    schema = {"type": "object", "properties": {"plan": {"type": "string"}}, "required": ["plan"]}
    malformed = ClaudeCodeClient(
        RecordingCommandClient(stdout="not-json"), "claude", 30  # type: ignore[arg-type]
    ).execute("Return JSON", tmp_path, schema)
    invalid = ClaudeCodeClient(
        RecordingCommandClient(stdout=_envelope(result="bad", structured_output={"plan": 2})),
        "claude",
        30,  # type: ignore[arg-type]
    ).execute("Return JSON", tmp_path, schema)

    assert not malformed.success
    assert malformed.error_message == "Claude Code returned malformed JSON output"
    assert malformed.workspace_changes_may_exist
    assert not invalid.success
    assert "invalid structured output" in (invalid.error_message or "")


def test_claude_code_client_marks_a_timeout_as_a_possible_workspace_change(tmp_path: Path) -> None:
    class TimedOutCommandClient:
        def run(self, command: list[str], **kwargs: Any) -> CommandResult:
            del command, kwargs
            raise CommandExecutionError("Command timed out after 30s: claude -p")

    client = ClaudeCodeClient(TimedOutCommandClient(), "claude", 30)  # type: ignore[arg-type]
    result = client.execute("work", tmp_path)

    assert not result.success
    assert result.workspace_changes_may_exist
    assert "timed out" in (result.error_message or "")


@pytest.mark.parametrize(
    ("message", "category"),
    [
        ("rate limit reached", "rate_limit"),
        ("quota exceeded", "quota"),
        ("service overloaded", "capacity"),
    ],
)
def test_claude_provider_preserves_retryable_fallback_categories(
    tmp_path: Path, message: str, category: str
) -> None:
    provider = ClaudeCodeAgentProvider(
        "claude",
        ClaudeCodeClient(
            RecordingCommandClient(exit_code=1, stdout=_envelope(result=message)),
            "claude",
            30,  # type: ignore[arg-type]
        ),
    )

    with pytest.raises(AgentProviderError) as caught:
        provider.execute(AgentRequest(instructions="work", title="work", workspace_path=tmp_path))

    assert caught.value.category == category


def test_claude_provider_exposes_completion_session_without_startup_resume(tmp_path: Path) -> None:
    provider = ClaudeCodeAgentProvider(
        "claude",
        ClaudeCodeClient(
            RecordingCommandClient(stdout=_envelope(result="done", session_id="session-123")),
            "claude",
            30,  # type: ignore[arg-type]
        ),
    )
    started: list[object] = []

    result = provider.execute(
        AgentRequest(instructions="work", title="work", workspace_path=tmp_path),
        on_started=started.append,
    )

    assert started == []
    assert result.execution is not None
    assert result.execution.external_id == "session-123"


@pytest.mark.parametrize(
    "executable",
    ["claude --resume session-123", "claude --worktree", "claude --add-dir other"],
)
def test_claude_settings_reject_cli_arguments_that_escape_provider_ownership(
    executable: str,
) -> None:
    with pytest.raises(ValidationError, match="one command entry"):
        ClaudeCodeProviderSettings.model_validate({"type": "claude-code", "executable": executable})


def test_claude_settings_reject_arbitrary_command_configuration() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ClaudeCodeProviderSettings.model_validate(
            {"type": "claude-code", "command": ["claude", "--dangerously-skip-permissions"]}
        )


def test_claude_settings_support_runtime_model_override_and_first_class_label(
    tmp_path: Path,
) -> None:
    config = ProjectConfig.model_validate(
        {
            "path": tmp_path,
            "agents": {
                "planner": {"provider": "claude"},
                "implementer": {"provider": "claude"},
                "reviewer": {"provider": "claude"},
            },
            "providers": {
                "claude": {
                    "type": "claude-code",
                    "model": "claude-configured",
                    "permission_mode": "acceptEdits",
                    "max_turns": 5,
                }
            },
        }
    )

    overridden = config.with_agent_runtime_overrides({}, {"planner": {"model": "claude-other"}})

    assert config.provider_options() == [("claude", "claude")]
    assert config.provider_runtime_values("claude") == {"model": "claude-configured"}
    assert overridden.provider_runtime_values("__tui_runtime_planner") == {"model": "claude-other"}
