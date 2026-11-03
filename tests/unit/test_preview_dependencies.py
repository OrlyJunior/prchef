from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "_preview_dependencies",
    Path(__file__).resolve().parents[2] / "scripts" / "_preview_dependencies.py",
)
assert _spec and _spec.loader
preview = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preview)


def _write_project(root: Path, *, name: str = "example", dependency: str = "a") -> None:
    root.mkdir()
    (root / "pyproject.toml").write_text(
        f'[build-system]\nrequires = ["build"]\n[project]\nname = "{name}"\n'
        f'requires-python = ">=3.11"\ndependencies = ["{dependency}"]\n',
        encoding="utf-8",
    )
    (root / "constraints-dev.txt").write_text("a==1\n", encoding="utf-8")


def test_unrelated_metadata_does_not_warn(tmp_path: Path) -> None:
    control, target = tmp_path / "control", tmp_path / "target"
    _write_project(control)
    _write_project(target, name="different")
    assert preview.dependency_warning(control, target) is None


@pytest.mark.parametrize("change", ["dependency", "constraints", "dynamic", "missing"])
def test_dependency_changes_and_unknowns_warn(tmp_path: Path, change: str) -> None:
    control, target = tmp_path / "control", tmp_path / "target"
    _write_project(control)
    _write_project(target, dependency="b" if change == "dependency" else "a")
    if change == "constraints":
        (target / "constraints-dev.txt").write_text("a==2\n", encoding="utf-8")
    elif change == "dynamic":
        with (target / "pyproject.toml").open("a", encoding="utf-8") as stream:
            stream.write('dynamic = ["dependencies"]\n')
    elif change == "missing":
        (target / "constraints-dev.txt").unlink()
    assert "current environment" in preview.dependency_warning(control, target)
