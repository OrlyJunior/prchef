from __future__ import annotations

import tomllib
from pathlib import Path


def test_core_dependencies_include_pyjwt_cryptography_support() -> None:
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject_path.open("rb") as pyproject_file:
        project = tomllib.load(pyproject_file)["project"]

    assert "PyJWT[crypto]>=2.8,<3" in project["dependencies"]


def test_litellm_is_a_core_dependency_not_an_optional_extra() -> None:
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject_path.open("rb") as pyproject_file:
        project = tomllib.load(pyproject_file)["project"]

    assert "litellm>=1.0.0" in project["dependencies"]
    assert "litellm" not in project.get("optional-dependencies", {})
