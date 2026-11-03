"""Read-only database discovery and snapshotting for the development preview.

This module deliberately does not use resolve_database_url: that function creates
the selected database's parent directory as part of normal application startup.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import yaml
from pydantic import ValidationError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from prchef.config.project_config import resolve_global_config
from prchef.config.settings import Settings
from prchef.config.state import default_state_directory


class PreviewDatabaseError(ValueError):
    """The selected database cannot safely be copied for a preview."""


def _configured_database(config: Path) -> tuple[str | None, str | None]:
    """Read the same global persistence keys as the normal resolver, without side effects."""
    try:
        raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PreviewDatabaseError(
            f"Cannot read persistence configuration {config}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise PreviewDatabaseError("Configuration root must be a mapping")
    defaults = raw.get("defaults", {})
    if not isinstance(defaults, dict):
        raise PreviewDatabaseError("'defaults' must be a mapping")
    persistence = defaults.get("persistence", {})
    if persistence is None:
        persistence = {}
    if not isinstance(persistence, dict):
        raise PreviewDatabaseError("'defaults.persistence' must be a mapping")
    url = persistence.get("database_url", defaults.get("database_url"))
    path = persistence.get("database_path", defaults.get("database_path"))
    if url is not None and path is not None:
        raise PreviewDatabaseError("Configure only one database URL or path.")
    return (str(url).strip() if url is not None else None,
            str(path).strip() if path is not None else None)


def _path(value: str | Path, base: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _sqlite_path(value: str, base: Path) -> Path:
    try:
        url = make_url(value)
    except ArgumentError as exc:
        raise PreviewDatabaseError("Preview requires a file-backed SQLite database.") from exc
    sqlite_driver = url.drivername == "sqlite" or url.drivername.startswith("sqlite+")
    if not sqlite_driver or not url.database:
        raise PreviewDatabaseError(
            "Preview requires a file-backed SQLite database; "
            "server and in-memory databases are unsupported."
        )
    database = url.database
    if database == ":memory:":
        raise PreviewDatabaseError(
            "Preview requires a file-backed SQLite database; in-memory databases are unsupported."
        )
    if database.startswith("file:"):
        uri = urlsplit(database)
        query = dict(parse_qs(uri.query))
        query.update({key: [str(value)] for key, value in url.query.items()})
        if uri.path in {"", ":memory:"} or query.get("mode", [""])[0] == "memory":
            raise PreviewDatabaseError(
                "Preview requires a file-backed SQLite database; "
                "in-memory databases are unsupported."
            )
        if uri.netloc not in {"", "localhost"}:
            raise PreviewDatabaseError("Preview cannot copy a non-local SQLite URI.")
        database = unquote(uri.path)
    return _path(database, base)


def resolve_source() -> tuple[Path, Path]:
    """Resolve the selected global config and database without writing either."""
    config = resolve_global_config()
    base = config.parent
    try:
        settings = Settings()
    except (ValidationError, ValueError, OSError) as exc:
        raise PreviewDatabaseError(f"Cannot read preview database settings: {exc}") from exc
    env_url = os.environ.get("PRCHEF_DATABASE_URL", "").strip()
    env_path = os.environ.get("PRCHEF_DATABASE_PATH", "").strip()
    if env_url and env_path:
        raise PreviewDatabaseError(
            "Configure only one of PRCHEF_DATABASE_URL or PRCHEF_DATABASE_PATH."
        )
    if env_url:
        return config, _sqlite_path(env_url, base)
    if env_path:
        return config, _path(env_path, base)
    if settings.database_url and settings.database_path:
        raise PreviewDatabaseError("Configure only one database URL or path.")
    if settings.database_url:
        return config, _sqlite_path(settings.database_url, base)
    if settings.database_path:
        return config, _path(settings.database_path, base)
    configured_url, configured_path = _configured_database(config)
    if configured_url:
        return config, _sqlite_path(configured_url, base)
    if configured_path:
        return config, _path(configured_path, base)
    legacy = base / "prchef.db"
    database = legacy if legacy.is_file() else default_state_directory() / "prchef.db"
    return config, database.resolve()


def snapshot(source: Path, destination: Path) -> bool:
    """Copy committed SQLite state, including WAL contents; return False if absent."""
    if not source.exists() and not source.is_symlink():
        return False
    created = False
    try:
        # Never overwrite a pre-existing file, even if backup fails partway through.
        with destination.open("xb"):
            created = True
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(destination)) as copy:
                original.backup(copy)
    except (OSError, sqlite3.Error) as exc:
        if created:
            destination.unlink(missing_ok=True)
        raise PreviewDatabaseError(f"Cannot copy SQLite database {source}: {exc}") from exc
    return True
