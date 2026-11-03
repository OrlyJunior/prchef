from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

_spec = importlib.util.spec_from_file_location(
    "_preview_database", Path(__file__).resolve().parents[2] / "scripts" / "_preview_database.py"
)
assert _spec and _spec.loader
preview = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preview)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://localhost/database",
        "sqlite://",
        "sqlite:///:memory:",
        "sqlite:///file::memory:?cache=shared&uri=true",
        "sqlite:///file:shared?mode=memory&cache=shared&uri=true",
    ],
)
def test_refuses_non_file_database(url: str, tmp_path: Path) -> None:
    with pytest.raises(preview.PreviewDatabaseError, match="file-backed SQLite"):
        preview._sqlite_path(url, tmp_path)


def test_relative_url_does_not_create_source_directory(tmp_path: Path) -> None:
    assert preview._sqlite_path("sqlite:///missing/history.db", tmp_path) == (
        tmp_path / "missing" / "history.db"
    )
    assert not (tmp_path / "missing").exists()


def test_snapshot_includes_committed_wal_and_preserves_source(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "copy.db"
    connection = sqlite3.connect(source)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE history (value TEXT)")
        connection.execute("INSERT INTO history VALUES ('committed')")
        connection.commit()
        assert preview.snapshot(source, destination)
        with sqlite3.connect(destination) as copy:
            assert copy.execute("SELECT value FROM history").fetchall() == [("committed",)]
            copy.execute("INSERT INTO history VALUES ('preview')")
        assert connection.execute("SELECT value FROM history").fetchall() == [("committed",)]
    finally:
        connection.close()


def test_missing_source_does_not_create_database(tmp_path: Path) -> None:
    destination = tmp_path / "copy.db"
    assert not preview.snapshot(tmp_path / "missing.db", destination)
    assert not destination.exists()


def test_settings_validation_error_is_explained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preview, "resolve_global_config", lambda: tmp_path / "prchef.yml")

    def invalid_settings() -> None:
        raise ValueError("invalid dotenv")

    monkeypatch.setattr(preview, "Settings", invalid_settings)
    with pytest.raises(preview.PreviewDatabaseError, match="invalid dotenv"):
        preview.resolve_source()


@pytest.mark.parametrize("kind", ["directory", "invalid"])
def test_invalid_source_reports_error_and_removes_partial_copy(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "source"
    if kind == "directory":
        source.mkdir()
    else:
        source.write_text("not a sqlite database", encoding="utf-8")
    destination = tmp_path / "copy.db"
    with pytest.raises(preview.PreviewDatabaseError, match="Cannot copy SQLite database"):
        preview.snapshot(source, destination)
    assert not destination.exists()


def test_snapshot_does_not_overwrite_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE history (value TEXT)")
    destination = tmp_path / "copy.db"
    destination.write_bytes(b"preserve")
    with pytest.raises(preview.PreviewDatabaseError, match="Cannot copy SQLite database"):
        preview.snapshot(source, destination)
    assert destination.read_bytes() == b"preserve"


def test_environment_selection_precedes_dotenv_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "prchef.yml"
    config.write_text("defaults: {}\n", encoding="utf-8")
    monkeypatch.setattr(preview, "resolve_global_config", lambda: config)
    monkeypatch.setattr(
        preview,
        "Settings",
        lambda: SimpleNamespace(database_url="postgresql://server/db", database_path=None),
    )
    monkeypatch.setenv("PRCHEF_DATABASE_PATH", "local/history.db")
    monkeypatch.delenv("PRCHEF_DATABASE_URL", raising=False)
    assert preview.resolve_source() == (config, tmp_path / "local" / "history.db")
    assert not (tmp_path / "local").exists()


def test_dotenv_server_url_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "prchef.yml"
    config.write_text("defaults: {}\n", encoding="utf-8")
    monkeypatch.setattr(preview, "resolve_global_config", lambda: config)
    monkeypatch.setattr(
        preview,
        "Settings",
        lambda: SimpleNamespace(database_url="postgresql://server/db", database_path=None),
    )
    monkeypatch.delenv("PRCHEF_DATABASE_URL", raising=False)
    monkeypatch.delenv("PRCHEF_DATABASE_PATH", raising=False)
    with pytest.raises(preview.PreviewDatabaseError, match="file-backed SQLite"):
        preview.resolve_source()


def test_configured_database_and_legacy_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "prchef.yml"
    monkeypatch.setattr(preview, "resolve_global_config", lambda: config)
    monkeypatch.setattr(
        preview, "Settings", lambda: SimpleNamespace(database_url=None, database_path=None)
    )
    monkeypatch.delenv("PRCHEF_DATABASE_URL", raising=False)
    monkeypatch.delenv("PRCHEF_DATABASE_PATH", raising=False)
    config.write_text(
        "defaults:\n  persistence:\n    database_path: data/history.db\n", encoding="utf-8"
    )
    assert preview.resolve_source()[1] == tmp_path / "data" / "history.db"
    config.write_text("defaults: {}\n", encoding="utf-8")
    legacy = tmp_path / "prchef.db"
    legacy.touch()
    assert preview.resolve_source()[1] == legacy


def test_conflicting_environment_overrides_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "prchef.yml"
    monkeypatch.setattr(preview, "resolve_global_config", lambda: config)
    monkeypatch.setattr(
        preview, "Settings", lambda: SimpleNamespace(database_url=None, database_path=None)
    )
    monkeypatch.setenv("PRCHEF_DATABASE_URL", "sqlite:///history.db")
    monkeypatch.setenv("PRCHEF_DATABASE_PATH", "other.db")
    with pytest.raises(preview.PreviewDatabaseError, match="only one"):
        preview.resolve_source()
