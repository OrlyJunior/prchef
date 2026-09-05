from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.config.project_config import (
    CodexCliProviderSettings,
    ConfigurationError,
    GitHubAppSettings,
    ProjectConfig,
    ProjectConfigLoader,
)
from orchestrator.models.development_task import DevelopmentTask


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


def test_workspace_root_is_configurable(tmp_path: Path) -> None:
    config = ProjectConfig.model_validate(
        {"path": tmp_path, "workspace": {"root": "C:/Git/orchestrator-workspaces"}}
    )

    assert str(config.workspace.root).replace("\\", "/").endswith(
        "C:/Git/orchestrator-workspaces"
    )


def test_max_validation_retries_defaults_to_two_and_rejects_negative_values(tmp_path: Path) -> None:
    config = ProjectConfig.model_validate({"path": tmp_path})

    assert config.validation.max_validation_retries == 2
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        ProjectConfig.model_validate(
            {"path": tmp_path, "validation": {"max_validation_retries": -1}}
        )


def test_max_review_retries_defaults_to_one_and_rejects_negative_values(tmp_path: Path) -> None:
    config = ProjectConfig.model_validate({"path": tmp_path})

    assert config.review.max_review_retries == 1
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        ProjectConfig.model_validate(
            {"path": tmp_path, "review": {"max_review_retries": -1}}
        )


def test_notifications_are_disabled_by_default_and_normalize_the_channel(tmp_path: Path) -> None:
    config = ProjectConfig.model_validate(
        {"path": tmp_path, "notifications": {"enabled": True, "channel": " Desktop "}}
    )

    assert config.notifications.enabled is True
    assert config.notifications.channel == "desktop"
    assert ProjectConfig.model_validate({"path": tmp_path}).notifications.enabled is False


def test_notification_configuration_uses_repository_and_local_overrides(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    (repository / ".orchestrator.yml").write_text(
        "notifications:\n  channel: desktop\n",
        encoding="utf-8",
    )
    config_file = tmp_path / "orchestrator.yml"
    config_file.write_text(
        """defaults:
  notifications:
    enabled: false
    channel: ignored
repositories:
  example:
    path: repository
    notifications:
      enabled: true
      channel: desktop
""",
        encoding="utf-8",
    )
    task = DevelopmentTask(
        id="1",
        source="trello",
        title="Task",
        description="",
        url="https://example.test/1",
        repository="example",
    )

    config = ProjectConfigLoader(config_file).load(task)

    assert config.notifications.enabled is True
    assert config.notifications.channel == "desktop"


def test_github_app_configuration_accepts_exactly_one_private_key_source(tmp_path: Path) -> None:
    config = ProjectConfig.model_validate(
        {
            "path": tmp_path,
            "integrations": {
                "github": {
                    "command": ["gh"],
                    "app": {
                        "app_id": 123,
                        "installation_id": 456,
                        "private_key_env": "GITHUB_APP_PRIVATE_KEY",
                    },
                }
            },
        }
    )

    assert config.integrations.github.app.is_configured
    assert config.integrations.github.app.app_id == 123


@pytest.mark.parametrize(
    "app",
    [
        {"app_id": 123, "installation_id": 456},
        {
            "app_id": 123,
            "installation_id": 456,
            "private_key_env": "KEY",
            "private_key_path": "key.pem",
        },
    ],
)
def test_github_app_configuration_rejects_incomplete_or_ambiguous_credentials(
    app: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="GitHub App"):
        GitHubAppSettings.model_validate(app)


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


def test_legacy_roles_and_integrations_are_migrated_to_named_providers(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, {"approval_policy": "approve-for-me"})

    assert config.agents.planner.provider == "jules"
    assert config.agents.implementer.provider == "codex"
    assert config.agents.reviewer.provider == "jules"
    codex = CodexCliProviderSettings.model_validate(
        config.providers["codex"].model_dump()
    )
    assert codex.approval_policy == "approve-for-me"


def test_legacy_codex_runtime_settings_are_migrated_to_named_provider(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        {"model": "legacy-model", "reasoning_effort": "high"},
    )
    codex = CodexCliProviderSettings.model_validate(config.providers["codex"].model_dump())

    assert codex.model == "legacy-model"
    assert codex.reasoning_effort == "high"


def test_new_agent_configuration_keeps_provider_options_scoped_by_name(
    tmp_path: Path,
) -> None:
    config = ProjectConfig.model_validate(
        {
            "path": tmp_path,
            "agents": {
                "planner": {"provider": "planner"},
                "implementer": {"provider": "worker"},
                "reviewer": {"provider": "planner"},
            },
            "providers": {
                "planner": {"type": "jules", "poll_interval_seconds": 9},
                "worker": {
                    "type": "codex-cli",
                    "command": ["codex", "exec", "-"],
                    "approval_policy": "approve-for-me",
                },
            },
        }
    )

    assert config.providers["planner"].model_dump()["poll_interval_seconds"] == 9
    assert config.providers["worker"].model_dump()["approval_policy"] == "approve-for-me"


def test_explicit_agent_configuration_takes_precedence_over_legacy_roles(
    tmp_path: Path,
) -> None:
    config = ProjectConfig.model_validate(
        {
            "path": tmp_path,
            "roles": {"spec": "jules", "implementation": "codex", "review": "jules"},
            "integrations": {
                "jules": {"poll_interval_seconds": 5},
                "codex": {
                    "command": ["codex", "exec", "-"],
                    "model": "legacy-model",
                    "reasoning_effort": "low",
                },
            },
            "agents": {
                "planner": {"provider": "vertex-gemini"},
                "implementer": {"provider": "codex"},
                "reviewer": {"provider": "vertex-gemini"},
            },
            "providers": {
                "vertex-gemini": {
                    "type": "gemini-vertex",
                    "project": "example-project",
                    "model": "gemini-test-model",
                },
                "codex": {
                    "type": "codex-cli",
                    "command": ["codex", "exec", "-"],
                    "model": "modern-model",
                    "reasoning_effort": "high",
                },
            },
        }
    )

    assert config.agents.planner.provider == "vertex-gemini"
    assert config.agents.implementer.provider == "codex"
    assert config.agents.reviewer.provider == "vertex-gemini"
    assert config.providers["vertex-gemini"].type == "gemini-vertex"
    codex = CodexCliProviderSettings.model_validate(config.providers["codex"].model_dump())
    assert codex.model == "modern-model"
    assert codex.reasoning_effort == "high"


@pytest.mark.parametrize(
    "partial_configuration",
    [
        {"agents": {"planner": {"provider": "vertex-gemini"}}},
        {"providers": {"vertex-gemini": {"type": "gemini-vertex"}}},
    ],
)
def test_partial_modern_agent_configuration_fails_instead_of_using_legacy_defaults(
    tmp_path: Path, partial_configuration: dict[str, object]
) -> None:
    with pytest.raises(ValidationError, match="Modern agent configuration requires both"):
        ProjectConfig.model_validate({"path": tmp_path, **partial_configuration})


def test_explicit_agent_configuration_requires_every_role(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="Explicit agents configuration is missing roles"):
        ProjectConfig.model_validate(
            {
                "path": tmp_path,
                "agents": {"planner": {"provider": "vertex-gemini"}},
                "providers": {
                    "vertex-gemini": {
                        "type": "gemini-vertex",
                        "project": "example-project",
                        "model": "gemini-test-model",
                    }
                },
            }
        )


def test_loader_rejects_project_settings_misplaced_at_global_top_level(tmp_path: Path) -> None:
    config_file = tmp_path / "orchestrator.yml"
    config_file.write_text(
        """task:\n  provider: github\nagents:\n  planner:\n    provider: vertex-gemini\n""",
        encoding="utf-8",
    )
    task = DevelopmentTask(
        id="1",
        source="github",
        title="Task",
        description="Description",
        url="https://example.test/issues/1",
        repository="owner/repo",
    )

    with pytest.raises(ConfigurationError, match="Project settings at the top level"):
        ProjectConfigLoader(config_file).load(task)
