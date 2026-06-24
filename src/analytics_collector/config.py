from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    allowed_origins: list[str] = Field(default_factory=list)
    analytics_site_id: str | None = None
    rate_limit_per_minute: int = 60
    mongo_url: str = "mongodb://localhost:27017"
    mongo_database: str = "analytics"
    mongo_collection: str = "analytics_events"
    ip_hash_salt: str = "change-me"
    ipinfo_lite_token: str | None = None
    geo_lookup_timeout_seconds: float = 1.0
    geo_lookup_debug: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
