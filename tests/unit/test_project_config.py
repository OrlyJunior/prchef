from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.config.project_config import ProjectConfig


def _config(tmp_path: Path, codex: dict[str, object]) -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            "path": tmp_path,
            "integrations": {
                "codex": {
                    "command": [
                        "codex",
                        "exec",
                        "--sandbox",
                        "workspace-write",
                        "--output-last-message",
                        "{output_file}",
                        "-",
                    ],
                    **codex,
                }
            },
        }
    )


@pytest.mark.parametrize("codex", [{}, {"approval_policy": None}])
def test_codex_approval_policy_is_disabled_by_default(
    tmp_path: Path, codex: dict[str, object]
) -> None:
    config = _config(tmp_path, codex)

    assert config.integrations.codex.approval_policy is None


def test_default_codex_config_keeps_workspace_write_sandbox(tmp_path: Path) -> None:
    config = ProjectConfig.model_validate({"path": tmp_path})

    assert config.integrations.codex.command[:4] == [
        "codex",
        "exec",
        "--sandbox",
        "workspace-write",
    ]
    assert "--approve-for-me" not in config.integrations.codex.command


def test_codex_approval_policy_accepts_approve_for_me(tmp_path: Path) -> None:
    config = _config(tmp_path, {"approval_policy": "approve-for-me"})

    assert config.integrations.codex.approval_policy == "approve-for-me"


def test_codex_approval_policy_rejects_unknown_values(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="approval_policy"):
        _config(tmp_path, {"approval_policy": "always-approve"})


def test_approve_for_me_rejects_a_non_workspace_write_sandbox(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="requires the workspace-write sandbox"):
        ProjectConfig.model_validate(
            {
                "path": tmp_path,
                "integrations": {
                    "codex": {
                        "command": ["codex", "exec", "--sandbox", "read-only", "-"],
                        "approval_policy": "approve-for-me",
                    }
                },
            }
        )
