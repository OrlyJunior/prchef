from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from orchestrator.clients.factory import ClientFactory
from orchestrator.config.project_config import (
    AgentPresetConfig,
    CodexCliProviderSettings,
    ConfigurationError,
    GitHubAppSettings,
    ProjectConfig,
    ProjectConfigLoader,
    ProviderSupportTier,
    resolve_global_config,
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


def test_repository_context_strategy_defaults_to_heuristic(tmp_path: Path) -> None:
    config = ProjectConfig.model_validate({"path": tmp_path})

    assert config.repository_context.strategy == "heuristic"


def test_repository_context_strategy_rejects_unknown_name(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as exc_info:
        ProjectConfig.model_validate(
            {"path": tmp_path, "repository_context": {"strategy": "semantic"}}
        )

    message = str(exc_info.value)
    assert "semantic" in message
    assert "heuristic" in message


def test_repository_context_strategy_uses_defaults_and_repository_override(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    config_file = tmp_path / "orchestrator.yml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "defaults": {"repository_context": {"strategy": "heuristic"}},
                "repositories": {
                    "example": {
                        "path": str(repository),
                        "repository_context": {"strategy": "heuristic"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    task = DevelopmentTask(
        id="context",
        source="test",
        title="Context strategy",
        description="",
        url="",
        repository="example",
    )

    config = ProjectConfigLoader(config_file).load(task)

    assert config.repository_context.strategy == "heuristic"


def test_loader_wraps_unknown_repository_context_strategy_as_configuration_error(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    config_file = tmp_path / "orchestrator.yml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "defaults": {"repository_context": {"strategy": "semantic"}},
                "repositories": {"example": {"path": str(repository)}},
            }
        ),
        encoding="utf-8",
    )
    task = DevelopmentTask(
        id="context",
        source="test",
        title="Context strategy",
        description="",
        url="",
        repository="example",
    )

    with pytest.raises(ConfigurationError) as exc_info:
        ProjectConfigLoader(config_file).load(task)

    message = str(exc_info.value)
    assert "semantic" in message
    assert "heuristic" in message


def test_provider_options_label_and_deprioritize_deprecated_jules(tmp_path: Path) -> None:
    with pytest.warns(DeprecationWarning, match="Jules provider"):
        config = ProjectConfig.model_validate(
            {
                "path": tmp_path,
                "agents": {
                    "planner": {"provider": "legacy"},
                    "implementer": {"provider": "codex"},
                    "reviewer": {"provider": "api"},
                },
                "providers": {
                    "legacy": {"type": "jules"},
                    "codex": {"type": "codex-cli", "command": ["codex", "exec", "-"]},
                    "api": {"type": "litellm", "model": "openai/example"},
                    "broken": {"type": "unknown-backend"},
                },
            }
        )

    assert config.provider_options() == [
        ("api", "api"),
        ("codex", "codex"),
        ("legacy (deprecated)", "legacy"),
        ("broken (unsupported)", "broken"),
    ]
    assert ProviderSupportTier.FIRST_CLASS.value == "first-class"
    assert ProviderSupportTier.OPTIONAL.value == "optional"
    assert ProviderSupportTier.DEPRECATED.value == "deprecated/legacy"
    assert ProviderSupportTier.UNSUPPORTED.value == "unsupported"


def test_example_configuration_uses_first_class_litellm_and_codex(tmp_path: Path) -> None:
    example_path = Path(__file__).resolve().parents[2] / "orchestrator.example.yml"
    example = yaml.safe_load(example_path.read_text(encoding="utf-8"))

    config = ProjectConfig.model_validate({"path": tmp_path, **example["defaults"]})

    assert config.agents.planner.provider == "cloud-api"
    assert config.agents.implementer.provider == "codex"
    assert config.agents.reviewer.provider == "cloud-api"
    assert config.providers["cloud-api"].type == "litellm"
    assert config.providers["codex"].type == "codex-cli"


def test_role_runtime_overrides_are_validated_in_memory_and_keep_defaults(tmp_path: Path) -> None:
    config = ProjectConfig.model_validate(
        {
            "path": tmp_path,
            "providers": {
                "codex": {
                    "type": "codex-cli",
                    "command": ["codex", "exec", "-"],
                    "model": "configured-model",
                    "reasoning_effort": "medium",
                },
                "vertex": {
                    "type": "gemini-vertex",
                    "project": "example-project",
                    "model": "configured-gemini",
                    "generation": {"temperature": 0.2},
                },
                "jules": {"type": "jules"},
            },
            "agents": {
                "planner": {"provider": "vertex"},
                "implementer": {"provider": "codex"},
                "reviewer": {"provider": "jules"},
            },
        }
    )

    overridden = config.with_agent_runtime_overrides(
        {"implementer": "codex"},
        {
            "implementer": {"model": "free-form-model", "reasoning_effort": "high"},
            "planner": {
                "model": "another-gemini-model",
                "generation": {"temperature": "0.8", "max_output_tokens": "512"},
            },
        },
    )

    assert config.agents.implementer.provider == "codex"
    assert config.provider_runtime_values("codex") == {
        "model": "configured-model",
        "reasoning_effort": "medium",
    }
    assert overridden.agents.implementer.provider == "__tui_runtime_implementer"
    assert overridden.provider_runtime_values("__tui_runtime_implementer") == {
        "model": "free-form-model",
        "reasoning_effort": "high",
    }
    assert overridden.provider_runtime_values("__tui_runtime_planner") == {
        "model": "another-gemini-model",
        "generation": {"temperature": 0.8, "max_output_tokens": 512},
    }


def test_litellm_role_runtime_materialization_preserves_backend_and_vertex_settings(
    tmp_path: Path,
) -> None:
    config = ProjectConfig.model_validate(
        {
            "path": tmp_path,
            "agents": {
                "planner": {"provider": "vertex-gemini-litellm"},
                "implementer": {"provider": "codex"},
                "reviewer": {"provider": "vertex-gemini-litellm"},
            },
            "providers": {
                "vertex-gemini-litellm": {
                    "type": "litellm",
                    "model": "vertex_ai/gemini-3.8-flash",
                    "generation": {
                        "vertex_project": "agents-orchestrator-507215",
                        "vertex_location": "global",
                    },
                },
                "codex": {"type": "codex-cli", "command": ["codex", "exec", "-"]},
            },
        }
    )

    selected = config.with_agent_runtime_overrides(
        {"planner": "vertex-gemini-litellm", "reviewer": "vertex-gemini-litellm"},
        {
            "planner": {"model": "vertex_ai/gemini-3.8-flash"},
            "reviewer": {"model": "vertex_ai/gemini-3.8-flash"},
        },
    )

    for role in ("planner", "reviewer"):
        runtime_name = getattr(selected.agents, role).provider
        settings = selected.providers[runtime_name].model_dump()
        assert settings["type"] == "litellm"
        assert settings["model"] == "vertex_ai/gemini-3.8-flash"
        assert settings["generation"] == {
            "vertex_project": "agents-orchestrator-507215",
            "vertex_location": "global",
        }


def test_role_runtime_overrides_reject_unsupported_and_invalid_values(tmp_path: Path) -> None:
    config = ProjectConfig.model_validate({"path": tmp_path})

    with pytest.raises(ConfigurationError, match="does not support runtime option"):
        config.with_agent_runtime_overrides({}, {"planner": {"model": "not-supported"}})
    with pytest.raises(ConfigurationError, match="must be a non-empty string"):
        config.with_agent_runtime_overrides(
            {}, {"implementer": {"reasoning_effort": " "}}
        )


def test_agent_presets_are_role_scoped_and_apply_as_runtime_templates(tmp_path: Path) -> None:
    config = ProjectConfig.model_validate(
        {
            "path": tmp_path,
            "providers": {
                "codex": {
                    "type": "codex-cli",
                    "command": ["codex", "exec", "-"],
                    "model": "configured-model",
                },
                "jules": {"type": "jules"},
            },
            "agents": {
                "planner": {"provider": "jules"},
                "implementer": {"provider": "codex"},
                "reviewer": {"provider": "jules"},
            },
            "agent_presets": {
                "implementer": {
                    "deep": {
                        "provider": "codex",
                        "model": "free-form-model",
                        "reasoning_effort": "xhigh",
                    }
                },
                "reviewer": {"default": {"provider": "jules"}},
            },
        }
    )

    assert set(config.presets_for_role("implementer")) == {"deep"}
    assert config.presets_for_role("planner") == {}
    assert set(config.presets_for_role("reviewer")) == {"default"}
    selected = config.with_agent_preset("implementer", "deep")
    assert selected.agents.implementer.provider == "__tui_runtime_implementer"
    assert selected.provider_runtime_values("__tui_runtime_implementer") == {
        "model": "free-form-model",
        "reasoning_effort": "xhigh",
    }
    assert config.provider_runtime_values("codex") == {
        "model": "configured-model",
        "reasoning_effort": None,
    }


def test_role_fallback_preset_is_optional_and_resolves_its_provider(tmp_path: Path) -> None:
    config = ProjectConfig.model_validate(
        {
            "path": tmp_path,
            "providers": {
                "primary": {"type": "jules"},
                "backup": {
                    "type": "litellm",
                    "model": "openai/default-model",
                },
            },
            "agents": {
                "planner": {"provider": "primary", "fallback_preset": "cloud"},
                "implementer": {"provider": "primary"},
                "reviewer": {"provider": "primary"},
            },
            "agent_presets": {
                "planner": {
                    "cloud": {"provider": "backup", "model": "openai/backup-model"}
                }
            },
        }
    )

    assert config.agents.planner.fallback_preset == "cloud"
    assert config.agents.implementer.fallback_preset is None
    fallback = config.with_agent_preset("planner", "cloud")
    fallback_settings = fallback.providers[fallback.agents.planner.provider]
    assert fallback_settings.type == "litellm"
    assert fallback.provider_runtime_values(fallback.agents.planner.provider)["model"] == (
        "openai/backup-model"
    )


@pytest.mark.parametrize(
    ("agent_presets", "message"),
    [
        ({}, "not configured"),
        (
            {"planner": {"cloud": {"provider": "missing"}}},
            "unconfigured provider",
        ),
    ],
)
def test_role_fallback_preset_rejects_invalid_targets(
    tmp_path: Path, agent_presets: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ProjectConfig.model_validate(
            {
                "path": tmp_path,
                "providers": {"primary": {"type": "jules"}},
                "agents": {
                    "planner": {"provider": "primary", "fallback_preset": "cloud"},
                    "implementer": {"provider": "primary"},
                    "reviewer": {"provider": "primary"},
                },
                "agent_presets": agent_presets,
            }
        )


def test_missing_preset_provider_is_retained_and_rejected_only_when_selected(
    tmp_path: Path,
) -> None:
    config = ProjectConfig.model_validate(
        {
            "path": tmp_path,
            "agent_presets": {"planner": {"retired": {"provider": "missing"}}},
        }
    )

    assert config.presets_for_role("planner")["retired"].provider == "missing"
    with pytest.raises(ConfigurationError, match="is not configured"):
        config.with_agent_preset("planner", "retired")


def test_agent_preset_supports_provider_generation_options(tmp_path: Path) -> None:
    config = ProjectConfig.model_validate(
        {
            "path": tmp_path,
            "providers": {
                "vertex": {
                    "type": "gemini-vertex",
                    "project": "example-project",
                    "model": "configured-model",
                }
            },
            "agents": {
                "planner": {"provider": "vertex"},
                "implementer": {"provider": "vertex"},
                "reviewer": {"provider": "vertex"},
            },
            "agent_presets": {
                "planner": {
                    "creative": {
                        "provider": "vertex",
                        "model": "free-form-model",
                        "generation": {"temperature": 0.8, "max_output_tokens": 512},
                    }
                }
            },
        }
    )

    selected = config.with_agent_preset("planner", "creative")
    assert selected.provider_runtime_values("__tui_runtime_planner") == {
        "model": "free-form-model",
        "generation": {"temperature": 0.8, "max_output_tokens": 512},
    }


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


def test_repository_configuration_ignores_legacy_local_override_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    (repository / ".orchestrator.yml").write_text(
        "notifications:\n  enabled: false\n  channel: ignored\n",
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
    assert "Repository-local .orchestrator.yml is no longer supported and is ignored" in caplog.text


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


def test_implementer_and_reviewer_github_apps_are_independent(tmp_path: Path) -> None:
    config = ProjectConfig.model_validate(
        {
            "path": tmp_path,
            "integrations": {
                "github": {
                    "command": ["gh"],
                    "app": {
                        "app_id": 123,
                        "installation_id": 456,
                        "private_key_env": "REVIEWER_APP_KEY",
                    },
                    "implementer_app": {
                        "app_id": 789,
                        "installation_id": 987,
                        "private_key_env": "IMPLEMENTER_APP_KEY",
                    },
                }
            },
        }
    )

    clients = ClientFactory(None).create(config, agent_roles=set())

    assert config.integrations.github.app.app_id == 123
    assert config.integrations.github.implementer_app.app_id == 789
    assert clients.git._implementer_authenticator is clients.github._implementer_authenticator
    assert (
        ProjectConfig.model_validate({"path": tmp_path})
        .integrations.github.implementer_app.is_configured
        is False
    )


def test_tui_configuration_writer_updates_resolved_global_config_and_preserves_other_content(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    config_file = tmp_path / "alternate-orchestrator.yml"
    loader = ProjectConfigLoader(config_file)
    config_file.write_text(
        """unrelated:
  keep: this value
defaults:
  notifications:
    enabled: false
repositories:
  example:
    path: repository
    existing: value
  other:
    path: another-repository
""",
        encoding="utf-8",
    )

    saved = loader.save_github_app_settings(
        "example",
        implementer_app=GitHubAppSettings(
            app_id=789,
            installation_id=987,
            private_key_env="IMPLEMENTER_APP_KEY",
        ),
        reviewer_app=GitHubAppSettings(),
    )

    assert saved == config_file
    assert not (repository / ".orchestrator.yml").exists()
    content = saved.read_text(encoding="utf-8")
    assert "implementer_app:" in content
    assert "IMPLEMENTER_APP_KEY" in content
    assert "private_key:" not in content
    persisted = yaml.safe_load(content)
    assert persisted["unrelated"] == {"keep": "this value"}
    assert persisted["repositories"]["example"]["existing"] == "value"
    assert persisted["repositories"]["other"] == {"path": "another-repository"}
    resolved = ProjectConfigLoader(config_file).load(
        DevelopmentTask(
            "1", "trello", "Task", "", "https://example.test/1", repository="example"
        )
    )
    assert resolved.integrations.github.implementer_app.app_id == 789


def test_repository_crud_round_trips_commands_and_preserves_configuration(
    tmp_path: Path,
) -> None:
    existing_path = tmp_path / "existing"
    existing_path.mkdir()
    config_file = tmp_path / "orchestrator.yml"
    config_file.write_text(
        """# global configuration comment
defaults:
  validation:
    auto_detect: false
  notifications:
    enabled: true
providers_metadata: # unrelated inline comment
  keep: exactly
repositories:
  first:
    path: existing
    integrations:
      custom: {enabled: true}
  last:
    path: elsewhere
""",
        encoding="utf-8",
    )
    loader = ProjectConfigLoader(config_file)

    loader.save_repository(
        "created",
        "new/repository",
        True,
        [["python", "-m", "pytest"], ["python", "-m", "mypy", "src/orchestrator"]],
    )
    loader.save_repository(
        "renamed",
        "existing",
        False,
        [["python", "-m", "ruff", "check", "."]],
        original_key="first",
    )

    content = config_file.read_text(encoding="utf-8")
    persisted = yaml.safe_load(content)
    assert "# global configuration comment" in content
    assert "# unrelated inline comment" in content
    assert persisted["providers_metadata"] == {"keep": "exactly"}
    assert list(persisted["repositories"]) == ["renamed", "last", "created"]
    assert persisted["repositories"]["renamed"]["integrations"] == {
        "custom": {"enabled": True}
    }
    assert persisted["repositories"]["created"]["validation"] == {
        "auto_detect": True,
        "commands": [
            ["python", "-m", "pytest"],
            ["python", "-m", "mypy", "src/orchestrator"],
        ],
    }
    settings = loader.repository_settings("created")
    assert settings.path == "new/repository"
    assert settings.commands[0] == ["python", "-m", "pytest"]
    assert settings.path_exists is False

    loader.delete_repository("renamed")
    assert existing_path.is_dir()
    assert "renamed" not in yaml.safe_load(config_file.read_text(encoding="utf-8"))[
        "repositories"
    ]


def test_invalid_repository_input_does_not_change_configuration(tmp_path: Path) -> None:
    config_file = tmp_path / "orchestrator.yml"
    original = "repositories:\n  example:\n    path: repository\n"
    config_file.write_text(original, encoding="utf-8")
    loader = ProjectConfigLoader(config_file)

    with pytest.raises(ConfigurationError, match="at least one argument"):
        loader.save_repository("example", "repository", True, [[]])

    assert config_file.read_text(encoding="utf-8") == original


def test_agent_preset_crud_persists_in_resolved_config_without_touching_providers(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    config_file = tmp_path / "orchestrator.yml"
    config_file.write_text(
        """defaults:
  agents:
    planner: {provider: jules}
    implementer: {provider: codex}
    reviewer: {provider: jules}
  providers:
    jules: {type: jules}
    codex: {type: codex-cli, command: [codex, exec, -]}
repositories:
  example:
    path: repository
""",
        encoding="utf-8",
    )
    loader = ProjectConfigLoader(config_file)

    loader.save_agent_preset(
        "example",
        "implementer",
        "fast",
        AgentPresetConfig(provider="codex", model="free-form", reasoning_effort="medium"),
    )
    loader.save_agent_preset(
        "example",
        "implementer",
        "fast",
        AgentPresetConfig(provider="codex", model="updated", reasoning_effort="xhigh"),
        overwrite=True,
    )
    loader.rename_agent_preset("example", "implementer", "fast", "deep")

    persisted = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert persisted["repositories"]["example"]["agent_presets"] == {
        "implementer": {
            "deep": {
                "provider": "codex",
                "model": "updated",
                "reasoning_effort": "xhigh",
            }
        }
    }
    assert persisted["defaults"]["providers"]["codex"]["type"] == "codex-cli"
    assert not (repository / ".orchestrator.yml").exists()
    reloaded = loader.load(
        DevelopmentTask("1", "trello", "Task", "", "https://example.test/1", repository="example")
    )
    assert reloaded.presets_for_role("implementer")["deep"].reasoning_effort == "xhigh"

    loader.delete_agent_preset("example", "implementer", "deep")
    persisted = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert "agent_presets" not in persisted["repositories"]["example"]


def test_agent_fallback_preset_persistence_tracks_rename_and_explicit_disable(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    config_file = tmp_path / "orchestrator.yml"
    config_file.write_text(
        """defaults:
  agents:
    planner: {provider: jules}
    implementer: {provider: codex}
    reviewer: {provider: jules}
  providers:
    jules: {type: jules}
    codex: {type: codex-cli, command: [codex, exec, -]}
repositories:
  example:
    path: repository
""",
        encoding="utf-8",
    )
    loader = ProjectConfigLoader(config_file)
    loader.save_agent_preset(
        "example", "planner", "backup", AgentPresetConfig(provider="jules")
    )

    loader.save_agent_fallback_preset("example", "planner", "backup")
    loader.rename_agent_preset("example", "planner", "backup", "renamed")
    persisted = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert persisted["repositories"]["example"]["agents"]["planner"] == {
        "fallback_preset": "renamed"
    }

    loader.save_agent_fallback_preset("example", "planner", None)
    persisted = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert persisted["repositories"]["example"]["agents"]["planner"] == {
        "fallback_preset": None
    }


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


def test_global_config_discovery_walks_up_from_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for variable in (
        "ORCHESTRATOR_CONFIG",
        "ORCHESTRATOR_CONFIG_PATH",
        "ORCHESTRATOR_CONFIG_FILE",
    ):
        monkeypatch.delenv(variable, raising=False)
    config_file = tmp_path / "orchestrator.yml"
    config_file.write_text("defaults: {}\n", encoding="utf-8")
    nested_working_directory = tmp_path / "one" / "two"
    nested_working_directory.mkdir(parents=True)
    monkeypatch.chdir(nested_working_directory)

    assert ProjectConfigLoader().load_defaults().path == tmp_path


def test_explicit_global_config_takes_precedence_over_environment_and_discovery(
    tmp_path: Path,
) -> None:
    explicit = tmp_path / "explicit.yml"
    from_environment = tmp_path / "environment.yml"
    discovered = tmp_path / "orchestrator.yml"
    for config_file in (explicit, from_environment, discovered):
        config_file.write_text("defaults: {}\n", encoding="utf-8")

    assert (
        resolve_global_config(
            explicit,
            environ={"ORCHESTRATOR_CONFIG": str(from_environment)},
            cwd=tmp_path,
        )
        == explicit.resolve()
    )


def test_environment_global_config_takes_precedence_over_cwd_discovery(tmp_path: Path) -> None:
    from_environment = tmp_path / "environment.yml"
    discovered = tmp_path / "orchestrator.yml"
    from_environment.write_text("defaults: {}\n", encoding="utf-8")
    discovered.write_text("defaults: {}\n", encoding="utf-8")

    assert (
        resolve_global_config(
            environ={"ORCHESTRATOR_CONFIG_PATH": str(from_environment)}, cwd=tmp_path
        )
        == from_environment.resolve()
    )


def test_global_config_discovery_uses_user_config_locations(tmp_path: Path) -> None:
    user_config = (
        tmp_path / "home" / ".config" / "orchestrator" / "orchestrator.yml"
    )
    user_config.parent.mkdir(parents=True)
    user_config.write_text("defaults: {}\n", encoding="utf-8")
    working_directory = tmp_path / "isolated"
    working_directory.mkdir()

    assert (
        resolve_global_config(
            environ={},
            cwd=working_directory,
            home=tmp_path / "home",
            installation_dir=tmp_path / "installation",
        )
        == user_config.resolve()
    )


def test_global_config_discovery_reports_every_searched_location(tmp_path: Path) -> None:
    working_directory = tmp_path / "isolated" / "nested"
    working_directory.mkdir(parents=True)

    with pytest.raises(ConfigurationError, match="Global configuration not found") as exc_info:
        resolve_global_config(
            environ={},
            cwd=working_directory,
            home=tmp_path / "home",
            installation_dir=tmp_path / "installation",
        )

    message = str(exc_info.value)
    assert str(working_directory / "orchestrator.yml") in message
    assert (
        str(tmp_path / "home" / ".config" / "orchestrator" / "orchestrator.yml")
        in message
    )
