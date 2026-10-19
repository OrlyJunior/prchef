from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # A repository-local .env would make a pipx-installed command behave
        # differently for every working directory.  Keep optional dotenv
        # credentials in the same user-global state directory as persistence.
        env_file=Path.home() / ".prchef" / ".env",
        env_prefix="PRCHEF_",
        extra="ignore",
    )

    trello_api_key: str | None = None
    trello_token: str | None = None
    # Resolution lives in config.database so the runtime and Alembic use the
    # same stable, cwd-independent location.
    database_url: str | None = None
    database_path: Path | None = None
    # Environment-based configuration selection is intentionally handled by
    # resolve_global_config(), which preserves its documented precedence. The
    # private validation alias keeps BaseSettings from consuming a public
    # PRCHEF_CONFIG* variable before that resolver sees it.
    config_file: Path | None = Field(
        default=None,
        validation_alias="_PRCHEF_CONFIG_CLI_PATH",
    )
    log_level: str = "INFO"
