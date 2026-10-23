"""Opt-in authenticated coverage for the Claude Code planner path."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from prchef.clients.factory import ClientFactory
from prchef.config.project_config import ProjectConfig
from prchef.models.agent import AgentRole
from prchef.models.development_task import DevelopmentTask
from prchef.services.spec_service import SpecService

_LIVE_ENABLED = (
    os.getenv("PRCHEF_TEST_CLAUDE_CODE") == "1"
    and shutil.which("claude") is not None
    and bool(os.getenv("ANTHROPIC_API_KEY"))
)

pytestmark = pytest.mark.skipif(
    not _LIVE_ENABLED,
    reason=(
        "set PRCHEF_TEST_CLAUDE_CODE=1 with claude on PATH and ANTHROPIC_API_KEY "
        "to run authenticated Claude Code coverage"
    ),
)


class _EmptyRepositoryContext:
    def render(self, request: object) -> str:
        del request
        return "REPOSITORY CONTEXT: deliberately empty live-test workspace."


def test_authenticated_claude_code_executes_the_planner_role(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "claude-live@example.test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Claude live test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    (tmp_path / "README.md").write_text("# isolated Claude planner test\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True, text=True
    )
    before_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout
    before_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    before_remotes = subprocess.run(
        ["git", "remote"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout
    before_status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    config = ProjectConfig.model_validate(
        {
            "path": tmp_path,
            "agents": {
                "planner": {"provider": "claude-live"},
                "implementer": {"provider": "claude-live"},
                "reviewer": {"provider": "claude-live"},
            },
            "providers": {
                "claude-live": {
                    "type": "claude-code",
                    "executable": "claude",
                    "timeout_seconds": 180,
                    "permission_mode": "dontAsk",
                    "max_turns": 3,
                    "bare_mode": True,
                }
            },
        }
    )
    provider = ClientFactory().create(config).agent(AgentRole.PLANNER)
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
    after_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout
    after_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    after_remotes = subprocess.run(
        ["git", "remote"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout
    after_status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert after_head == before_head
    assert after_branch == before_branch
    assert after_remotes == before_remotes
    assert after_status == before_status
