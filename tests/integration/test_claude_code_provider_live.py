"""Opt-in authenticated coverage for the Claude Code planner path."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from orchestrator.clients.claude_code_client import ClaudeCodeClient
from orchestrator.clients.command_client import CommandClient
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.providers.claude_code_provider import ClaudeCodeAgentProvider
from orchestrator.services.spec_service import SpecService

_LIVE_ENABLED = (
    os.getenv("ORCHESTRATOR_TEST_CLAUDE_CODE") == "1"
    and shutil.which("claude") is not None
    and bool(os.getenv("ANTHROPIC_API_KEY"))
)

pytestmark = pytest.mark.skipif(
    not _LIVE_ENABLED,
    reason=(
        "set ORCHESTRATOR_TEST_CLAUDE_CODE=1 with claude on PATH and ANTHROPIC_API_KEY "
        "to run authenticated Claude Code coverage"
    ),
)


class _EmptyRepositoryContext:
    def render(self, request: object) -> str:
        del request
        return "REPOSITORY CONTEXT: deliberately empty live-test workspace."


def test_authenticated_claude_code_executes_the_planner_role(tmp_path: Path) -> None:
    provider = ClaudeCodeAgentProvider(
        "claude-live",
        ClaudeCodeClient(
            CommandClient(),
            "claude",
            180,
            permission_mode="dontAsk",
            max_turns=3,
            bare_mode=True,
            provider_name="claude-live",
        ),
    )
    service = SpecService(provider, _EmptyRepositoryContext())  # type: ignore[arg-type]
    task = DevelopmentTask(
        id="claude-live-planner",
        source="integration-test",
        title="Describe the empty workspace",
        description="Return a concise safe technical specification for this empty workspace.",
        url="https://example.test/claude-live-planner",
        repository="example/test",
    )

    spec = service.generate(
        task,
        tmp_path,
        "example/test",
        "main",
        on_execution_started=lambda reference: pytest.fail(
            f"Claude Code should expose sessions only after completion: {reference}"
        ),
    )

    assert spec.technical_context
    assert spec.current_behavior
    assert spec.implementation_proposal
    assert isinstance(spec.safe_to_implement, bool)
    assert list(tmp_path.iterdir()) == []
