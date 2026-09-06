from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

from orchestrator.config.database import Base, resolve_database_url
from orchestrator.repositories import orm_models  # noqa: F401

config = context.config

load_dotenv()
# ``sqlalchemy.url`` is populated by the application for programmatic runs.
# Manual Alembic commands leave it blank, in which case this uses the exact
# same global resolver as the application instead of the process cwd.
if not config.get_main_option("sqlalchemy.url").strip():
    config.set_main_option("sqlalchemy.url", resolve_database_url())

if config.config_file_name is not None:
    # Keep the orchestrator's application loggers enabled while Alembic
    # configures its own handlers for migration output.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
