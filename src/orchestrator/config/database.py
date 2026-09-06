from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import yaml
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from orchestrator.config.project_config import ConfigurationError, resolve_global_config


class PersistenceConfigurationError(RuntimeError):
    """Raised when the globally configured persistence location is invalid."""


_DATABASE_URL_ENVIRONMENT_VARIABLE = "ORCHESTRATOR_DATABASE_URL"
_DATABASE_PATH_ENVIRONMENT_VARIABLE = "ORCHESTRATOR_DATABASE_PATH"
_DEFAULT_STATE_DIRECTORY_NAME = ".orchestrator"
_DATABASE_FILENAME = "orchestrator.db"


class Base(DeclarativeBase):
    pass


def _default_state_directory(home: Path | None = None) -> Path:
    return (home or Path.home()).expanduser() / _DEFAULT_STATE_DIRECTORY_NAME


def _read_configured_database_location(config_file: Path) -> tuple[str | None, str | None]:
    """Read persistence settings from global defaults without involving a repository.

    Persistence is intentionally global.  Repository and local project settings
    are never considered while resolving this location.
    """
    try:
        raw = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PersistenceConfigurationError(
            f"Cannot read persistence configuration {config_file}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise PersistenceConfigurationError(
            f"Configuration root must be a mapping: {config_file}"
        )
    defaults = raw.get("defaults", {})
    if not isinstance(defaults, dict):
        raise PersistenceConfigurationError("'defaults' must be a mapping")

    persistence = defaults.get("persistence", {})
    if persistence is None:
        persistence = {}
    if not isinstance(persistence, dict):
        raise PersistenceConfigurationError("'defaults.persistence' must be a mapping")

    # The direct keys are accepted as a small compatibility convenience for
    # early configurations created before the persistence section was named.
    database_url = persistence.get("database_url", defaults.get("database_url"))
    database_path = persistence.get("database_path", defaults.get("database_path"))
    if database_url is not None and database_path is not None:
        raise PersistenceConfigurationError(
            "Configure only one of database_url or database_path for persistence"
        )
    return (
        str(database_url).strip() if database_url is not None else None,
        str(database_path).strip() if database_path is not None else None,
    )


def _resolve_path(value: str | Path, base_directory: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_directory / path
    return path.resolve()


def _sqlite_url_for_path(path: Path) -> str:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PersistenceConfigurationError(
            f"Cannot create database directory {path.parent}: {exc}"
        ) from exc
    return f"sqlite:///{path.as_posix()}"


def _prepare_sqlite_url(database_url: str, base_directory: Path) -> str:
    """Anchor relative SQLite URLs and create their parent directory.

    A relative ``sqlite:///database.db`` URL is otherwise interpreted by
    SQLAlchemy relative to the process cwd.  Treating it as relative to the
    stable configuration/state directory keeps explicit overrides predictable.
    """
    try:
        url = make_url(database_url)
    except ArgumentError as exc:
        raise PersistenceConfigurationError(
            f"Invalid database URL: {database_url!r}"
        ) from exc
    database = url.database
    if (
        not url.drivername.startswith("sqlite")
        or database is None
        or database in {"", ":memory:"}
    ):
        return database_url

    database_path = _resolve_path(database, base_directory)
    try:
        database_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PersistenceConfigurationError(
            f"Cannot create database directory {database_path.parent}: {exc}"
        ) from exc
    return url.set(database=database_path.as_posix()).render_as_string(hide_password=False)


def resolve_database_url(
    *,
    database_url: str | None = None,
    database_path: str | Path | None = None,
    config_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    cwd: Path | None = None,
) -> str:
    """Resolve the database URL independently of the process working directory.

    The precedence is environment URL/path, explicit application URL/path,
    global ``defaults.persistence`` settings, a legacy database adjacent to the
    discovered global configuration, then ``~/.orchestrator/orchestrator.db``.
    """
    environment = os.environ if environ is None else environ
    state_directory = _default_state_directory(home)
    discovered_config: Path | None = None
    try:
        discovered_config = resolve_global_config(
            config_file, environ=environment, cwd=cwd, home=home
        )
    except ConfigurationError:
        # A database can still be initialized before a command needs project
        # configuration.  Explicitly selected missing configuration remains
        # the responsibility of the normal configuration loader.
        pass

    base_directory = discovered_config.parent if discovered_config else state_directory
    environment_url = environment.get(_DATABASE_URL_ENVIRONMENT_VARIABLE, "").strip()
    environment_path = environment.get(_DATABASE_PATH_ENVIRONMENT_VARIABLE, "").strip()
    if environment_url and environment_path:
        raise PersistenceConfigurationError(
            "Configure only one of ORCHESTRATOR_DATABASE_URL or ORCHESTRATOR_DATABASE_PATH"
        )
    if environment_url:
        return _prepare_sqlite_url(environment_url, base_directory)
    if environment_path:
        return _sqlite_url_for_path(_resolve_path(environment_path, base_directory))
    if database_url and database_path:
        raise PersistenceConfigurationError("Configure only one of database_url or database_path")
    if database_url:
        return _prepare_sqlite_url(database_url, base_directory)
    if database_path:
        return _sqlite_url_for_path(_resolve_path(database_path, base_directory))

    if discovered_config is not None:
        configured_url, configured_path = _read_configured_database_location(discovered_config)
        if configured_url:
            return _prepare_sqlite_url(configured_url, discovered_config.parent)
        if configured_path:
            return _sqlite_url_for_path(
                _resolve_path(configured_path, discovered_config.parent)
            )
        legacy_database = discovered_config.parent / _DATABASE_FILENAME
        if legacy_database.is_file():
            return _sqlite_url_for_path(legacy_database.resolve())

    default_database = state_directory / _DATABASE_FILENAME
    return _sqlite_url_for_path(default_database.resolve())


def build_engine(database_url: str) -> Engine:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)

