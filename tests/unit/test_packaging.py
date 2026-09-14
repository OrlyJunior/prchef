from __future__ import annotations

import tomllib
from pathlib import Path


def test_core_dependencies_include_pyjwt_cryptography_support() -> None:
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject_path.open("rb") as pyproject_file:
        project = tomllib.load(pyproject_file)["project"]

    assert "PyJWT[crypto]>=2.8,<3" in project["dependencies"]


def test_packaging_exposes_the_standard_orchestrator_console_script() -> None:
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject_path.open("rb") as pyproject_file:
        project = tomllib.load(pyproject_file)["project"]

    assert project["scripts"]["orchestrator"] == "orchestrator.cli.main:app"


def test_packaging_keeps_migrations_inside_the_orchestrator_package() -> None:
    package_root = Path(__file__).resolve().parents[2] / "src" / "orchestrator"

    assert (package_root / "migrations" / "__init__.py").is_file()
    assert (package_root / "migrations" / "env.py").is_file()
    assert (
        package_root / "migrations" / "versions" / "0008_add_workflow_cancellation.py"
    ).is_file()


def test_litellm_is_a_core_dependency_not_an_optional_extra() -> None:
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject_path.open("rb") as pyproject_file:
        project = tomllib.load(pyproject_file)["project"]

    assert "litellm>=1.0.0" in project["dependencies"]
    assert "litellm" not in project.get("optional-dependencies", {})


def test_qdrant_and_local_embeddings_are_core_dependencies() -> None:
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject_path.open("rb") as pyproject_file:
        project = tomllib.load(pyproject_file)["project"]

    assert "qdrant-client[fastembed]>=1.12,<2" in project["dependencies"]
    assert "numpy>=1.26,<2.5" in project["dependencies"]
