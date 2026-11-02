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

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from prchef.config.database import _read_configured_database_location
from prchef.config.project_config import resolve_global_config
from prchef.config.settings import Settings
from prchef.config.state import default_state_directory


class PreviewDatabaseError(ValueError):
    """The selected database cannot safely be copied for a preview."""


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
    settings = Settings()
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
    configured_url, configured_path = _read_configured_database_location(config)
    if configured_url:
        return config, _sqlite_path(configured_url, base)
    if configured_path:
        return config, _path(configured_path, base)
    legacy = base / "prchef.db"
    database = legacy if legacy.is_file() else default_state_directory() / "prchef.db"
    return config, database.resolve()


def snapshot(source: Path, destination: Path) -> bool:
    """Copy committed SQLite state, including WAL contents; return False if absent."""
    if not source.exists():
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
