from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ORCHESTRATOR_",
        extra="ignore",
    )

    trello_api_key: str | None = None
    trello_token: str | None = None
    jules_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ORCHESTRATOR_JULES_API_KEY", "JULES_API_KEY"),
    )
    # Resolution lives in config.database so the runtime and Alembic use the
    # same stable, cwd-independent location.
    database_url: str | None = None
    database_path: Path | None = None
    # Environment-based configuration selection is intentionally handled by
    # resolve_global_config(), which preserves its documented precedence. The
    # private validation alias keeps BaseSettings from consuming a public
    # ORCHESTRATOR_CONFIG* variable before that resolver sees it.
    config_file: Path | None = Field(
        default=None,
        validation_alias="_ORCHESTRATOR_CONFIG_CLI_PATH",
    )
    log_level: str = "INFO"
