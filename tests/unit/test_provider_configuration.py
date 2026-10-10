from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

from orchestrator.config import provider_configuration as editor
from orchestrator.config.project_config import ConfigurationError, ProjectConfigLoader
from orchestrator.providers.concurrency import ProviderConcurrencyCoordinator


def definition(kind: str) -> dict[str, Any]:
    values = deepcopy(editor.PROVIDER_DEFAULTS[kind])
    values["type"] = kind
    if kind in {"litellm", "gemini-vertex"}:
        values["model"] = "test/model"
    if kind == "gemini-vertex":
        values["project"] = "test-project"
    return values


@pytest.mark.parametrize("kind", editor.PROVIDER_DEFAULTS)
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_provider_crud_preserves_remainder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, newline: str,
) -> None:
    monkeypatch.setattr(editor.shutil, "which", lambda executable: "/fake/executable")
    before = "# header\ndefaults:\n   task: {provider: 'github'} # keep\n   providers:\n"
    existing = "      'other': {type: litellm, model: \"other/model\"} # unchanged\n"
    after = (
        "   # unrelated settings\n   validation: {auto_detect: false}\n"
        "repositories:\n   repo: {path: './missing', providers: {other: {model: override}}}\n"
        "state: {path: 'keep.db'} # footer\n"
    )
    path = tmp_path / "orchestrator.yml"
    original = (before + existing + after).replace("\n", newline).encode()
    path.write_bytes(original)
    loader = ProjectConfigLoader(path)
    values = definition(kind)
    assert not loader.save_provider("new:provider", values, create=True)
    created = path.read_bytes()
    assert created.startswith((before + existing).replace("\n", newline).encode())
    assert created.endswith(after.replace("\n", newline).encode())
    assert loader.provider_settings()["new:provider"] == values
    editor.validate_provider(loader.provider_settings()["new:provider"])
    values["max_concurrency"] = 3
    loader.save_provider("new:provider", values)
    edited = path.read_bytes()
    assert edited.startswith((before + existing).replace("\n", newline).encode())
    assert edited.endswith(after.replace("\n", newline).encode())
    assert loader.provider_settings()["new:provider"]["max_concurrency"] == 3
    loader.delete_provider("new:provider")
    assert path.read_bytes() == original


@pytest.mark.parametrize("source", [
    "defaults: {providers: {}} # keep\nrepositories: {}\n",
    "defaults: {} # keep\nrepositories: {}\n",
    "# keep\nrepositories: {}\n",
    "defaults:\n  task: {provider: github}\nrepositories: {}\n",
    "defaults: {providers: {'a:b': {type: litellm, model: one}, "
    "other: {type: litellm, model: two}}} # keep\nrepositories: {}\n",
])
def test_empty_and_flow_containers(tmp_path: Path, source: str) -> None:
    path = tmp_path / "orchestrator.yml"
    path.write_bytes(source.encode())
    loader = ProjectConfigLoader(path)
    original = yaml.safe_load(source)
    loader.save_provider("new", definition("litellm"), create=True)
    expected = deepcopy(original)
    expected.setdefault("defaults", {}).setdefault("providers", {})["new"] = definition("litellm")
    assert yaml.safe_load(path.read_bytes()) == expected
    if "# keep" in source:
        assert b"# keep" in path.read_bytes()
    loader.delete_provider("new")
    expected["defaults"]["providers"].pop("new")
    assert yaml.safe_load(path.read_bytes()) == expected
    if "'a:b'" in source:
        loader.delete_provider("a:b")
        assert loader.provider_settings() == {"other": {"type": "litellm", "model": "two"}}


@pytest.mark.parametrize("kind,change", [
    ("codex-cli", {"command": []}),
    ("codex-cli", {"command": [True]}),
    ("codex-cli", {"command": "codex"}),
    ("codex-cli", {"command": ["codex", ""]}),
    ("codex-cli", {"command": ["codex", "--model=other"], "model": "test"}),
    ("codex-cli", {"command": ["codex", "-c", "model_reasoning_effort=low"],
                   "reasoning_effort": "high"}),
    ("codex-cli", {"command": ["codex", "--sandbox=read-only"],
                   "approval_policy": "approve-for-me"}),
    ("claude-code", {"executable": "claude --resume"}),
    ("claude-code", {"executable": ""}),
    ("claude-code", {"effort": "invalid"}),
    ("claude-code", {"permission_mode": "bypassPermissions"}),
    ("litellm", {"model": " "}),
    ("litellm", {"api_key_env": "actual-secret-value"}),
    ("gemini-vertex", {"project": " "}),
    ("gemini-vertex", {"model": ""}),
    ("gemini-vertex", {"location": " "}),
])
def test_invalid_options_never_save(tmp_path: Path, kind: str, change: dict[str, Any]) -> None:
    path = tmp_path / "orchestrator.yml"
    original = b"defaults: {providers: {}}\n"
    path.write_bytes(original)
    with pytest.raises(ConfigurationError):
        ProjectConfigLoader(path).save_provider("new", definition(kind) | change, create=True)
    assert path.read_bytes() == original


@pytest.mark.parametrize("field,value", [
    ("max_concurrency", 0), ("max_concurrency", -1), ("max_concurrency", 1.5),
    ("max_concurrency", True), ("max_concurrency", "2"), ("timeout_seconds", 0),
    ("timeout_seconds", -1), ("timeout_seconds", True), ("timeout_seconds", float("nan")),
])
@pytest.mark.parametrize("kind", editor.PROVIDER_DEFAULTS)
def test_invalid_numbers(tmp_path: Path, kind: str, field: str, value: Any) -> None:
    path = tmp_path / "orchestrator.yml"
    path.write_bytes(b"defaults: {providers: {}}\n")
    with pytest.raises(ConfigurationError, match=field):
        ProjectConfigLoader(path).save_provider(
            "new", definition(kind) | {field: value}, create=True,
        )
    assert path.read_bytes() == b"defaults: {providers: {}}\n"


def test_names_and_expected_revision(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.yml"
    path.write_bytes(b"defaults: {providers: {}}\n")
    loader = ProjectConfigLoader(path)
    for name in ("", " ", " padded "):
        with pytest.raises(ConfigurationError, match="name"):
            loader.save_provider(name, definition("litellm"), create=True)
    loader.save_provider("new", definition("litellm"), create=True)
    original = path.read_bytes()
    with pytest.raises(ConfigurationError, match="already exists"):
        loader.save_provider("new", definition("litellm"), create=True)
    with pytest.raises(ConfigurationError, match="no longer exists"):
        loader.save_provider("missing", definition("litellm"))
    path.write_bytes(original + b"# external edit\n")
    with pytest.raises(ConfigurationError, match="externally"):
        loader.save_provider("new", definition("litellm"), expected_source=original)
    assert path.read_bytes() == original + b"# external edit\n"


@pytest.mark.parametrize("change", [
    {"api_key": "RECOGNIZABLE_SECRET"},
    {"generation": {"api_key": "RECOGNIZABLE_SECRET"}},
    {"generation": {"nested": {"access_token": "RECOGNIZABLE_SECRET"}}},
    {"generation": {"api_key_env": "RECOGNIZABLE_SECRET"}},
    {"type": "codex-cli", "command": ["env", "OPENAI_API_KEY=RECOGNIZABLE_SECRET", "codex"]},
    {"type": "codex-cli", "command": ["codex", "--api-key", "RECOGNIZABLE_SECRET"]},
    {"type": "codex-cli", "command": ["codex", "--config=api_key=RECOGNIZABLE_SECRET"]},
    {"api_base": "https://user:RECOGNIZABLE_SECRET@example.test"},
])
def test_credentials_are_rejected_without_echo(tmp_path: Path, change: dict[str, Any]) -> None:
    path = tmp_path / "orchestrator.yml"
    path.write_bytes(b"defaults: {providers: {}}\n")
    with pytest.raises(ConfigurationError) as failure:
        ProjectConfigLoader(path).save_provider("new", definition("litellm") | change, create=True)
    assert "RECOGNIZABLE_SECRET" not in str(failure.value)
    assert path.read_bytes() == b"defaults: {providers: {}}\n"


def test_environment_reference_is_never_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "RECOGNIZABLE_SECRET")
    path = tmp_path / "orchestrator.yml"
    path.write_bytes(b"defaults: {providers: {}}\n")
    warnings = ProjectConfigLoader(path).save_provider(
        "api", definition("litellm") | {"api_key_env": "TEST_PROVIDER_KEY"}, create=True,
    )
    assert not warnings
    assert b"TEST_PROVIDER_KEY" in path.read_bytes()
    assert b"RECOGNIZABLE_SECRET" not in path.read_bytes()


@pytest.mark.parametrize("kind", ["codex-cli", "claude-code"])
@pytest.mark.parametrize("found", [True, False])
def test_path_warning_is_nonblocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, found: bool,
) -> None:
    import subprocess

    def no_process(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Provider editing must not execute subprocesses")

    monkeypatch.setattr(subprocess, "Popen", no_process)
    monkeypatch.setattr(editor.shutil, "which", lambda executable: executable if found else None)
    values = definition(kind)
    if kind == "codex-cli":
        values["command"][0] = "/custom/bin/codex"
    else:
        values["executable"] = "/custom/bin/claude"
    path = tmp_path / "orchestrator.yml"
    path.write_bytes(b"defaults: {providers: {}}\n")
    warnings = ProjectConfigLoader(path).save_provider("new", values, create=True)
    assert bool(warnings) is not found
    assert ProjectConfigLoader(path).provider_settings()["new"] == values


@pytest.mark.parametrize("scope,settings,reference", [
    ("defaults", {"agents": {"planner": {"provider": "api"}}}, "agents.planner.provider"),
    ("repository", {"agents": {"reviewer": {"provider": "api"}}}, "agents.reviewer.provider"),
    ("defaults", {"roles": {"spec": "api"}}, "roles.spec"),
    ("repository", {"roles": {"review": "api"}}, "roles.review"),
    ("defaults", {"agent_presets": {"implementer": {"fast": {"provider": "api"}}}},
     "agent_presets.implementer.fast.provider"),
    ("repository", {"agent_presets": {"reviewer": {"fallback": {"provider": "api"}}},
                    "agents": {"reviewer": {"provider": "other", "fallback_preset": "fallback"}}},
     "agent_presets.reviewer.fallback.provider"),
])
def test_in_use_refusal(
    tmp_path: Path, scope: str, settings: dict[str, Any], reference: str,
) -> None:
    data: dict[str, Any] = {"defaults": {"providers": {"api": definition("litellm")}}}
    if scope == "defaults":
        data["defaults"].update(settings)
        location = "defaults"
    else:
        data["repositories"] = {"example": settings}
        location = "repositories.example"
    path = tmp_path / "orchestrator.yml"
    original = yaml.safe_dump(data).encode()
    path.write_bytes(original)
    with pytest.raises(ConfigurationError, match=f"{location}.{reference}"):
        ProjectConfigLoader(path).delete_provider("api")
    assert path.read_bytes() == original


def test_implicit_legacy_implementer_and_inherited_references(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.yml"
    path.write_text("defaults:\n  providers: {codex: {type: litellm, model: one}}\n"
                    "  roles: {spec: other, review: other}\nrepositories:\n  example: {}\n")
    with pytest.raises(ConfigurationError) as failure:
        ProjectConfigLoader(path).delete_provider("codex")
    assert "defaults.roles.implementation" in str(failure.value)
    assert "repositories.example.roles.implementation" in str(failure.value)


@pytest.mark.parametrize("source", [
    "defaults: &config {providers: {}}\nother: *config\n",
    "defaults: {providers: {}}\nother: &recursive [*recursive]\n",
    "base: &base {}\ndefaults:\n  <<: *base\n  providers: {}\n",
    "defaults: {providers: {}, providers: {}}\n",
])
def test_unsafe_yaml_does_not_change(tmp_path: Path, source: str) -> None:
    path = tmp_path / "orchestrator.yml"
    path.write_bytes(source.encode())
    with pytest.raises(ConfigurationError):
        ProjectConfigLoader(path).save_provider("api", definition("litellm"), create=True)
    assert path.read_bytes() == source.encode()


@pytest.mark.parametrize("stage", [
    "serialization", "validation", "semantics", "temporary", "flush", "replace", "stale",
])
def test_save_failures_clean_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    path = tmp_path / "orchestrator.yml"
    original = b"defaults: {providers: {}}\n"
    path.write_bytes(original)

    def failure(*args: Any, **kwargs: Any) -> Any:
        raise OSError("injected failure")

    if stage == "serialization":
        monkeypatch.setattr(editor, "_dump", failure)
    elif stage == "validation":
        monkeypatch.setattr(editor, "_splice", lambda *args: "defaults: [invalid")
    elif stage == "semantics":
        monkeypatch.setattr(
            editor, "_splice", lambda *args: "defaults: {providers: {}}\nother: true",
        )
    elif stage == "temporary":
        monkeypatch.setattr(editor.tempfile, "NamedTemporaryFile", failure)
    elif stage == "flush":
        monkeypatch.setattr(editor.os, "fsync", failure)
    elif stage == "replace":
        monkeypatch.setattr(editor.os, "replace", failure)
    else:
        original_fsync = editor.os.fsync

        def external_edit(descriptor: int) -> None:
            original_fsync(descriptor)
            path.write_bytes(original + b"# external\n")

        monkeypatch.setattr(editor.os, "fsync", external_edit)
    with pytest.raises((ConfigurationError, OSError)):
        ProjectConfigLoader(path).save_provider("api", definition("litellm"), create=True)
    assert path.read_bytes() == original + (b"# external\n" if stage == "stale" else b"")
    assert list(tmp_path.iterdir()) == [path]


def test_preserved_options_and_preset_compatibility(tmp_path: Path) -> None:
    values = definition("claude-code") | {"bare_mode": True, "max_turns": 9}
    path = tmp_path / "orchestrator.yml"
    path.write_text(yaml.safe_dump({"defaults": {
        "providers": {"local": values},
        "agent_presets": {"planner": {"fast": {"provider": "local", "permission_mode": "auto"}}},
    }}))
    loader = ProjectConfigLoader(path)
    original = path.read_bytes()
    with pytest.raises(ConfigurationError, match="defaults.agent_presets.planner.fast"):
        loader.save_provider("local", definition("litellm"))
    assert path.read_bytes() == original
    loader.save_provider("local", values | {"model": "new-model"})
    assert loader.provider_settings()["local"]["bare_mode"] is True
    assert loader.provider_settings()["local"]["max_turns"] == 9


def test_existing_concurrency_remains_process_scoped(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.yml"
    path.write_bytes(
        b"defaults: {providers: {api: {type: litellm, model: one, max_concurrency: 1}}}\n"
    )
    coordinator = ProviderConcurrencyCoordinator({"api": 1})
    loader = ProjectConfigLoader(path)
    loader.save_provider("api", definition("litellm") | {"max_concurrency": 4})
    assert coordinator.register("api", loader.provider_settings()["api"]["max_concurrency"]) == 1
    assert coordinator.limit_for("api") == 1


def test_flow_edit_preserves_quoted_neighbor_and_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "orchestrator.yml"
    prefix = b"\xef\xbb\xbf# keep\r\ndefaults: { providers: { "
    selected = b"'target: key': {type: litellm, model: old}"
    suffix = (
        b", \"other\": {type: litellm, model: 'unchanged'} }, task: {provider: 'github'} }\r\n"
        b"repositories: {} # keep\r\n"
    )
    path.write_bytes(prefix + selected + suffix)
    loader = ProjectConfigLoader(path)
    loader.save_provider("target: key", definition("litellm"))
    assert path.read_bytes().startswith(prefix)
    assert path.read_bytes().endswith(suffix)
    loader.delete_provider("target: key")
    assert path.read_bytes().startswith(prefix)
    assert b'\"other\": {type: litellm, model: \'unchanged\'}' in path.read_bytes()
