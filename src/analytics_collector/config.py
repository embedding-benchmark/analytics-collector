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
    mongo_hourly_collection: str = "analytics_hourly_metrics"
    mongo_daily_collection: str = "analytics_daily_metrics"
    mongo_funnel_collection: str = "analytics_funnel_metrics"
    mongo_retention_collection: str = "analytics_retention_metrics"
    analytics_admin_token: str | None = None
    ip_hash_salt: str = "change-me"
    ipinfo_lite_token: str | None = None
    geo_lookup_timeout_seconds: float = 1.0
    geo_lookup_debug: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
