from __future__ import annotations

from pathlib import Path

from sqlalchemy.engine import make_url

from orchestrator.config.database import resolve_database_url


def _database_path(database_url: str) -> Path:
    database = make_url(database_url).database
    assert database is not None
    return Path(database)


def test_database_environment_url_overrides_global_persistence_configuration(
    tmp_path: Path,
) -> None:
    config = tmp_path / "orchestrator.yml"
    config.write_text(
        "defaults:\n  persistence:\n    database_path: configured.db\n",
        encoding="utf-8",
    )
    environment_database = tmp_path / "environment" / "workflow.db"

    resolved = resolve_database_url(
        config_file=config,
        environ={"ORCHESTRATOR_DATABASE_URL": f"sqlite:///{environment_database.as_posix()}"},
        home=tmp_path / "home",
        cwd=tmp_path / "unrelated",
    )

    assert _database_path(resolved) == environment_database.resolve()
    assert environment_database.parent.is_dir()


def test_database_configuration_path_is_resolved_from_global_config_not_cwd(tmp_path: Path) -> None:
    config_directory = tmp_path / "config"
    config_directory.mkdir()
    config = config_directory / "orchestrator.yml"
    config.write_text(
        "defaults:\n  persistence:\n    database_path: state/workflow.db\n",
        encoding="utf-8",
    )

    resolved = resolve_database_url(
        config_file=config,
        environ={},
        home=tmp_path / "home",
        cwd=tmp_path / "arbitrary-working-directory",
    )

    assert _database_path(resolved) == (config_directory / "state" / "workflow.db").resolve()


def test_database_uses_legacy_file_adjacent_to_discovered_global_config(tmp_path: Path) -> None:
    config = tmp_path / "orchestrator.yml"
    config.write_text("defaults: {}\n", encoding="utf-8")
    legacy_database = tmp_path / "orchestrator.db"
    legacy_database.touch()

    resolved = resolve_database_url(
        config_file=config,
        environ={},
        home=tmp_path / "home",
        cwd=tmp_path / "unrelated",
    )

    assert _database_path(resolved) == legacy_database.resolve()


def test_database_defaults_to_user_state_directory_independent_of_cwd(tmp_path: Path) -> None:
    home = tmp_path / "home"
    arbitrary_cwd = tmp_path / "arbitrary-working-directory"
    arbitrary_cwd.mkdir()

    resolved = resolve_database_url(environ={}, home=home, cwd=arbitrary_cwd)

    assert _database_path(resolved) == (home / ".orchestrator" / "orchestrator.db").resolve()
    assert (home / ".orchestrator").is_dir()
