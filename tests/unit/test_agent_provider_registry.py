import logging
from pathlib import Path

import pytest

from orchestrator.clients.factory import ClientFactory
from orchestrator.config.project_config import ConfigurationError, ProjectConfig
from orchestrator.models import AgentRole
from orchestrator.providers.claude_code_provider import ClaudeCodeAgentProvider
from orchestrator.providers.codex_provider import CodexAgentProvider
from orchestrator.providers.gemini_vertex_provider import GeminiVertexAgentProvider
from orchestrator.providers.jules_provider import JulesAgentProvider
from orchestrator.providers.litellm_provider import LiteLLMAgentProvider


def _config(
    tmp_path: Path,
    *,
    agents: dict[str, object],
    providers: dict[str, object],
) -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            "path": tmp_path,
            "agents": agents,
            "providers": providers,
        }
    )


def test_resolves_named_provider_types_and_reuses_shared_instances(tmp_path: Path) -> None:
    with pytest.warns(DeprecationWarning, match="Jules provider"):
        config = _config(
            tmp_path,
            agents={
                "planner": {"provider": "remote"},
                "implementer": {"provider": "local"},
                "reviewer": {"provider": "remote"},
            },
            providers={
                "remote": {"type": "jules", "poll_interval_seconds": 7},
                "local": {
                    "type": "codex-cli",
                    "command": ["codex", "exec", "-"],
                    "timeout_seconds": 45,
                },
            },
        )

    clients = ClientFactory("test-key").create(config)

    assert isinstance(clients.agent(AgentRole.PLANNER), JulesAgentProvider)
    assert isinstance(clients.agent(AgentRole.IMPLEMENTER), CodexAgentProvider)
    assert clients.agent(AgentRole.PLANNER) is clients.agent(AgentRole.REVIEWER)


def test_resolves_and_reuses_a_named_claude_code_provider(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        agents={
            "planner": {"provider": "claude"},
            "implementer": {"provider": "claude"},
            "reviewer": {"provider": "codex"},
        },
        providers={
            "claude": {"type": "claude-code", "executable": "claude", "effort": "xhigh"},
            "codex": {"type": "codex-cli", "command": ["codex", "exec", "-"]},
        },
    )

    clients = ClientFactory(None).create(config)

    planner = clients.agent(AgentRole.PLANNER)
    assert isinstance(planner, ClaudeCodeAgentProvider)
    assert planner is clients.agent(AgentRole.IMPLEMENTER)
    assert planner._client._effort == "xhigh"


def test_jules_instantiation_logs_its_deprecated_status(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with pytest.warns(DeprecationWarning, match="Jules provider"):
        config = _config(
            tmp_path,
            agents={
                "planner": {"provider": "legacy"},
                "implementer": {"provider": "codex"},
                "reviewer": {"provider": "legacy"},
            },
            providers={
                "legacy": {"type": "jules"},
                "codex": {"type": "codex-cli", "command": ["codex", "exec", "-"]},
            },
        )

    with caplog.at_level(logging.WARNING, logger="orchestrator"):
        ClientFactory("test-key").create(config, agent_roles={AgentRole.PLANNER})

    assert "uses deprecated backend 'jules'" in caplog.text


def test_different_roles_can_reference_different_named_instances(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        agents={
            "planner": {"provider": "planner-jules"},
            "implementer": {"provider": "codex"},
            "reviewer": {"provider": "review-jules"},
        },
        providers={
            "planner-jules": {"type": "jules", "poll_interval_seconds": 5},
            "review-jules": {"type": "jules", "poll_interval_seconds": 20},
            "codex": {
                "type": "codex-cli",
                "command": ["codex", "exec", "-"],
                "timeout_seconds": 45,
            },
        },
    )

    clients = ClientFactory("test-key").create(config)

    assert clients.agent(AgentRole.PLANNER) is not clients.agent(AgentRole.REVIEWER)


def test_litellm_backend_is_preserved_for_independent_planner_and_reviewer_roles(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        agents={
            "planner": {"provider": "vertex-gemini-litellm"},
            "implementer": {"provider": "codex"},
            "reviewer": {"provider": "vertex-gemini-litellm"},
        },
        providers={
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
    )

    clients = ClientFactory(None).create(config)

    assert isinstance(clients.agent(AgentRole.PLANNER), LiteLLMAgentProvider)
    assert isinstance(clients.agent(AgentRole.REVIEWER), LiteLLMAgentProvider)
    assert clients.agent(AgentRole.PLANNER) is clients.agent(AgentRole.REVIEWER)
    assert isinstance(clients.agent(AgentRole.IMPLEMENTER), CodexAgentProvider)


def test_legacy_gemini_vertex_backend_remains_available(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        agents={
            "planner": {"provider": "vertex-gemini"},
            "implementer": {"provider": "codex"},
            "reviewer": {"provider": "vertex-gemini"},
        },
        providers={
            "vertex-gemini": {
                "type": "gemini-vertex",
                "project": "example-project",
                "model": "gemini-3.8-flash",
            },
            "codex": {"type": "codex-cli", "command": ["codex", "exec", "-"]},
        },
    )

    clients = ClientFactory(None).create(config)

    assert isinstance(clients.agent(AgentRole.PLANNER), GeminiVertexAgentProvider)
    assert isinstance(clients.agent(AgentRole.REVIEWER), GeminiVertexAgentProvider)


def test_unknown_provider_name_has_an_actionable_error(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        agents={
            "planner": {"provider": "missing"},
            "implementer": {"provider": "codex"},
            "reviewer": {"provider": "codex"},
        },
        providers={"codex": {"type": "codex-cli", "command": ["codex", "exec", "-"]}},
    )

    with pytest.raises(ConfigurationError, match="Unknown agent provider name 'missing'"):
        ClientFactory("test-key").create(config, agent_roles={AgentRole.PLANNER})


def test_unknown_provider_type_has_an_actionable_error(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        agents={
            "planner": {"provider": "future"},
            "implementer": {"provider": "future"},
            "reviewer": {"provider": "future"},
        },
        providers={"future": {"type": "future-provider", "model": "future-model"}},
    )

    with pytest.raises(ConfigurationError, match="Unknown agent provider type 'future-provider'"):
        ClientFactory("test-key").create(config, agent_roles={AgentRole.PLANNER})


def test_unselected_provider_options_are_not_validated_by_another_provider(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        agents={
            "planner": {"provider": "jules"},
            "implementer": {"provider": "codex"},
            "reviewer": {"provider": "jules"},
        },
        providers={
            "jules": {"type": "jules", "poll_interval_seconds": 5},
            "codex": {"type": "codex-cli", "jules_only_option": True},
        },
    )

    clients = ClientFactory("test-key").create(
        config, agent_roles={AgentRole.PLANNER}
    )

    assert isinstance(clients.agent(AgentRole.PLANNER), JulesAgentProvider)
    with pytest.raises(ConfigurationError, match="Invalid configuration.*'codex'"):
        ClientFactory("test-key").create(
            config, agent_roles={AgentRole.IMPLEMENTER}
        )


def test_verbose_factory_logging_reports_the_final_role_resolution(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = _config(
        tmp_path,
        agents={
            "planner": {"provider": "remote"},
            "implementer": {"provider": "local"},
            "reviewer": {"provider": "remote"},
        },
        providers={
            "remote": {
                "type": "gemini-vertex",
                "project": "example-project",
                "model": "gemini-test-model",
            },
            "local": {"type": "codex-cli", "command": ["codex", "exec", "-"]},
        },
    )

    with caplog.at_level(logging.DEBUG, logger="orchestrator"):
        ClientFactory(None).create(config)

    assert (
        "planner -> remote (gemini-vertex); implementer -> local (codex-cli); "
        "reviewer -> remote (gemini-vertex)"
    ) in caplog.text
