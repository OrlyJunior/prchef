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
    database_url: str = "sqlite:///./orchestrator.db"
    config_file: Path = Field(default=Path("orchestrator.yml"))
    log_level: str = "INFO"
