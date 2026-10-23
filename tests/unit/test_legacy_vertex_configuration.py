from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

from prchef.clients.factory import ClientFactory
from prchef.config.project_config import (
    ConfigurationError,
    ProjectConfigLoader,
    convert_legacy_vertex_provider,
)
from prchef.models.agent import AgentRole
from prchef.models.development_task import DevelopmentTask
from prchef.providers.litellm_provider import LiteLLMAgentProvider


def _loader(tmp_path: Path, definition: dict[str, Any]) -> tuple[ProjectConfigLoader, Path]:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    data = {
        "defaults": {
            "agents": {role: {"provider": "vertex"}
                       for role in ("planner", "implementer", "reviewer")},
            "providers": {"vertex": definition},
            "repository_context": {"strategy": "heuristic"},
        },
        "repositories": {"example": {"path": str(checkout)}},
    }
    path = tmp_path / "prchef.yml"
    path.write_text("# preserve me\n" + yaml.safe_dump(data), encoding="utf-8")
    return ProjectConfigLoader(path), path


@pytest.mark.parametrize("model", ["gemini-test", "vertex_ai/gemini-test"])
def test_legacy_vertex_conversion_is_lossless_and_never_rewrites_yaml(
    tmp_path: Path, model: str,
) -> None:
    definition = {
        "type": "gemini-vertex", "model": model, "project": "example-project",
        "location": "us-central1", "max_concurrency": 3, "timeout_seconds": 42,
        "generation": {"temperature": 0.7, "max_output_tokens": 512},
    }
    original = deepcopy(definition)
    loader, path = _loader(tmp_path, definition)
    source = path.read_bytes()
    config = loader.load_defaults()
    assert config.providers["vertex"].model_dump() == {
        "type": "litellm", "model": "vertex_ai/gemini-test", "max_concurrency": 3,
        "timeout_seconds": 42, "temperature": 0.7, "max_output_tokens": 512,
        "generation": {"vertex_project": "example-project", "vertex_location": "us-central1"},
    }
    assert loader.provider_settings()["vertex"] == config.providers["vertex"].model_dump()
    assert path.read_bytes() == source
    assert definition == original
    # Saved executions containing only provider names still resolve without SDK/auth calls.
    resumed = config.with_agent_runtime_overrides({"planner": "vertex"}, {})
    clients = ClientFactory().create(resumed, agent_roles={AgentRole.PLANNER})
    assert isinstance(clients.agent(AgentRole.PLANNER), LiteLLMAgentProvider)
    assert resumed.agents.planner.provider == "vertex"


def test_legacy_defaults_and_warning_once_per_loader(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    loader, path = _loader(tmp_path, {
        "type": "gemini-vertex", "model": "gemini-test", "project": "example-project",
    })
    source = path.read_bytes()
    config = loader.load_defaults()
    loader.load_defaults()
    loader.provider_settings()
    task = DevelopmentTask("1", "github", "Task", "", "https://example.test/1")
    selected = loader.load(task, "example")
    options = config.providers["vertex"].model_dump()
    assert options["timeout_seconds"] == 900
    assert options["temperature"] == 0.2
    assert options["generation"]["vertex_location"] == "global"
    assert selected.providers["vertex"].type == "litellm"
    warnings = [record for record in caplog.records if "converted to litellm" in record.message]
    assert len(warnings) == 1
    assert "vertex" in warnings[0].message
    assert "not rewritten" in warnings[0].message
    assert path.read_bytes() == source


@pytest.mark.parametrize("change,field", [
    ({"unexpected": True}, "unexpected"),
    ({"generation": {"top_p": 0.9}}, "generation.top_p"),
    ({"generation": {"temperature": 3}}, "generation.temperature"),
    ({"generation": {"max_output_tokens": 0}}, "generation.max_output_tokens"),
    ({"generation": []}, "generation"),
    ({"project": " "}, "project"),
    ({"model": ""}, "model"),
    ({"location": ""}, "location"),
    ({"timeout_seconds": 1.5}, "timeout_seconds"),
    ({"max_concurrency": True}, "max_concurrency"),
])
def test_unmappable_legacy_fields_fail_with_provider_and_field_without_writes(
    tmp_path: Path, change: dict[str, Any], field: str,
) -> None:
    loader, path = _loader(tmp_path, {
        "type": "gemini-vertex", "model": "gemini-test", "project": "example-project", **change,
    })
    source = path.read_bytes()
    with pytest.raises(ConfigurationError) as caught:
        loader.load_defaults()
    assert "provider 'vertex'" in str(caught.value)
    assert repr(field) in str(caught.value)
    assert path.read_bytes() == source


def test_current_provider_conversion_is_idempotent() -> None:
    definition = {"type": "litellm", "model": "vertex_ai/gemini-test",
                  "generation": {"vertex_project": "example-project", "top_p": 0.9}}
    assert convert_legacy_vertex_provider("vertex", definition) == definition


def test_legacy_repository_overrides_are_merged_before_conversion(tmp_path: Path) -> None:
    loader, path = _loader(tmp_path, {
        "type": "gemini-vertex", "model": "gemini-default", "project": "example-project",
        "generation": {"temperature": 0.5},
    })
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["repositories"]["example"]["providers"] = {
        "vertex": {"model": "gemini-repository", "location": "us-central1",
                   "generation": {"max_output_tokens": 256}},
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    source = path.read_bytes()
    task = DevelopmentTask("1", "github", "Task", "", "https://example.test/1")
    options = loader.load(task, "example").providers["vertex"].model_dump()
    assert options["model"] == "vertex_ai/gemini-repository"
    assert options["temperature"] == 0.5
    assert options["max_output_tokens"] == 256
    assert options["generation"] == {
        "vertex_project": "example-project", "vertex_location": "us-central1",
    }
    assert path.read_bytes() == source
