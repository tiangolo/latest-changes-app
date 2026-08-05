from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    github_client_id: str
    github_app_private_key: SecretStr
    github_webhook_secret: SecretStr


@lru_cache
def get_settings() -> Settings:
    return Settings()
