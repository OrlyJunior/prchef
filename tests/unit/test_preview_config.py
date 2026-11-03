from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

_spec = importlib.util.spec_from_file_location(
    "_preview_config", Path(__file__).resolve().parents[2] / "scripts" / "_preview_config.py"
)
assert _spec and _spec.loader
preview = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preview)


def test_copy_anchors_repositories_and_redirects_all_storage(tmp_path: Path) -> None:
    source = tmp_path / "source" / "prchef.yml"
    source.parent.mkdir()
    original = {
        "defaults": {
            "database_url": "postgresql://real/db",
            "repository_context": {"qdrant": {"mode": "server", "host": "real"}},
            "workspace": {"root": "/real/workspaces"},
            "providers": {"provider": {"token_env": "SECRET"}},
        },
        "repositories": {
            "one": {"path": "../repository", "repository_context": {
                "qdrant": {"mode": "server", "url": "http://real"}
            }},
            "two": {"path": "../other", "workspace": {"root": "/real/other"}},
        },
    }
    source.write_text(yaml.safe_dump(original), encoding="utf-8")
    destination = tmp_path / "preview.yml"
    root = tmp_path / "session"
    preview.copy_configuration(source, destination, root)
    copied = yaml.safe_load(destination.read_text(encoding="utf-8"))
    assert yaml.safe_load(source.read_text(encoding="utf-8")) == original
    assert copied["repositories"]["one"]["path"] == str(tmp_path / "repository")
    defaults = copied["defaults"]
    assert defaults["persistence"] == {"database_path": str(root / "history.db")}
    assert "database_url" not in defaults
    assert defaults["providers"] == original["defaults"]["providers"]
    for entry in [defaults, *copied["repositories"].values()]:
        assert entry["workspace"]["root"] == str(root / "workspaces")
        assert entry["repository_context"]["qdrant"] == {
            "mode": "local", "path": str(root / "qdrant")
        }


def test_copy_refuses_to_overwrite_and_rejects_invalid_shapes(tmp_path: Path) -> None:
    source = tmp_path / "prchef.yml"
    source.write_text("defaults: {}\n", encoding="utf-8")
    destination = tmp_path / "copy.yml"
    destination.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        preview.copy_configuration(source, destination, tmp_path)
    assert destination.read_text(encoding="utf-8") == "keep"
    with pytest.raises(ValueError, match="repositories"):
        preview.isolated_document({"repositories": []}, source, tmp_path)
