from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from prchef.config.project_config import (
    ConfigurationError,
    ProjectConfig,
    ProjectConfigLoader,
)
from prchef.models.development_task import DevelopmentTask


def config(tmp_path: Path, provider: dict[str, Any]) -> ProjectConfig:
    return ProjectConfig.model_validate({
        "path": tmp_path,
        "providers": {"agent": provider},
        "agents": {role: {"provider": "agent"} for role in ("planner", "implementer", "reviewer")},
        "agent_presets": {role: {"focused": {
            "provider": "agent", "model": "preset-model", "reasoning_effort": "high",
            "permission_mode": "acceptEdits",
        }} for role in ("planner", "implementer", "reviewer")},
    })


@pytest.mark.parametrize("role", ["planner", "implementer", "reviewer"])
def test_runtime_sources_use_submission_resolution_without_mutation(
    tmp_path: Path, role: str,
) -> None:
    original = config(tmp_path, {"type": "claude-code", "model": "provider-model", "effort": "low"})
    before = original.model_dump()
    inherited = original.role_runtime_value_sources(role, "agent", {
        "model": "provider-model", "reasoning_effort": "low",
    })
    assert (inherited["model"].value, inherited["model"].source) == ("provider-model", "provider")
    assert inherited["reasoning_effort"].source == "provider"
    assert (inherited["permission_mode"].value, inherited["permission_mode"].source) == (
        "dontAsk", "default",
    )
    preset = original.presets_for_role(role)["focused"]
    options = preset.runtime_options()
    sources = original.role_runtime_value_sources(role, "agent", options, preset_name="focused")
    assert {item.source for item in sources.values()} == {"preset"}
    options["model"] = "launch-model"
    sources = original.role_runtime_value_sources(role, "agent", options, preset_name="focused")
    assert sources["model"].source == "launch"
    assert sources["reasoning_effort"].source == "preset"
    submitted = original.with_agent_runtime_overrides({role: "agent"}, {role: options})
    assert {key: item.value for key, item in sources.items()} == submitted.provider_runtime_values(
        getattr(submitted.agents, role).provider
    )
    assert original.model_dump() == before


def test_runtime_sources_explicit_permission_and_cleared_model(tmp_path: Path) -> None:
    original = config(tmp_path, {
        "type": "claude-code", "model": "configured", "permission_mode": "dontAsk",
    })
    assert original.role_runtime_value_sources("planner", "agent", {})[
        "permission_mode"
    ].source == "provider"
    cleared = original.role_runtime_value_sources("planner", "agent", {"model": None})
    assert (cleared["model"].value, cleared["model"].source) == (None, "launch")
    assert cleared["reasoning_effort"].source == "default"


def test_runtime_sources_read_codex_command_flags_without_changing_controls(tmp_path: Path) -> None:
    original = config(tmp_path, {
        "type": "codex-cli",
        "command": ["codex", "exec", "--model=command-model", "-c", "model_reasoning_effort=high"],
    })
    sources = original.role_runtime_value_sources("implementer", "agent", {
        "model": None, "reasoning_effort": None,
    })
    assert (sources["model"].value, sources["model"].source) == ("command-model", "provider")
    assert (sources["reasoning_effort"].value, sources["reasoning_effort"].source) == (
        "high", "provider",
    )
    assert "permission_mode" not in sources
    assert original.provider_runtime_values("agent")["model"] is None
    with pytest.raises(ConfigurationError, match="conflicting model"):
        original.role_runtime_value_sources("implementer", "agent", {"model": "different"})


def test_runtime_sources_api_and_invalid_values_share_normal_validation(tmp_path: Path) -> None:
    original = config(tmp_path, {"type": "litellm", "model": "example/model"})
    sources = original.role_runtime_value_sources("planner", "agent", {"model": "example/model"})
    assert set(sources) == {"model"}
    assert sources["model"].source == "provider"
    with pytest.raises(ConfigurationError, match="permission_mode"):
        original.role_runtime_value_sources("planner", "agent", {"permission_mode": "auto"})
    with pytest.raises(ConfigurationError, match="does not select"):
        original.role_runtime_value_sources("planner", "agent", {}, preset_name="missing")
    with pytest.raises(ConfigurationError, match="Unknown agent role"):
        original.role_runtime_value_sources("other", "agent", {})


def test_runtime_sources_include_repository_overrides_without_writing_yaml(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    path = tmp_path / "orchestrator.yml"
    original = (
        b"defaults:\n  providers:\n    agent: {type: claude-code, model: global}\n"
        b"  agents:\n    planner: {provider: agent}\n    implementer: {provider: agent}\n"
        b"    reviewer: {provider: agent}\nrepositories:\n  example:\n    path: .\n"
        b"    providers:\n      agent: {model: local}\n"
    )
    path.write_bytes(original)
    loaded = ProjectConfigLoader(path).load(
        DevelopmentTask("1", "github", "Example", "", "", repository="example")
    )
    sources = loaded.role_runtime_value_sources("planner", "agent", {"model": "local"})
    assert (sources["model"].value, sources["model"].source) == ("local", "provider")
    assert path.read_bytes() == original
