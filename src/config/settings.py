"""Environment-backed application settings."""

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


DEFAULT_CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]


class Settings(BaseSettings):
    """Validated runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    microsoft_app_id: str = Field(
        default="", validation_alias="MICROSOFT_APP_ID"
    )
    microsoft_app_password: str = Field(
        default="", validation_alias="MICROSOFT_APP_PASSWORD"
    )
    microsoft_app_tenant_id: str = Field(
        default="", validation_alias="MICROSOFT_APP_TENANT_ID"
    )
    microsoft_app_type: str = Field(
        default="SingleTenant", validation_alias="MICROSOFT_APP_TYPE"
    )
    log_level: str = "INFO"
    app_env: str = Field(default="development", validation_alias="APP_ENV")
    mongodb_url: str = Field(default="", validation_alias="MONGODB_URL")
    mongodb_database: str = Field(
        default="enterprise_risk_bot", validation_alias="MONGODB_DATABASE"
    )
    mongodb_server_selection_timeout_ms: int = Field(
        default=2000, validation_alias="MONGODB_SERVER_SELECTION_TIMEOUT_MS"
    )
    mongodb_connect_timeout_ms: int = Field(
        default=3000, validation_alias="MONGODB_CONNECT_TIMEOUT_MS"
    )
    mongodb_socket_timeout_ms: int = Field(
        default=5000, validation_alias="MONGODB_SOCKET_TIMEOUT_MS"
    )
    teams_debug_activity_logging: bool = Field(
        default=False, validation_alias="TEAMS_DEBUG_ACTIVITY_LOGGING"
    )
    hourly_card_update_worker_enabled: bool = Field(
        default=False, validation_alias="HOURLY_CARD_UPDATE_WORKER_ENABLED"
    )
    hourly_card_update_batch_size: int = Field(
        default=100, ge=1, le=500, validation_alias="HOURLY_CARD_UPDATE_BATCH_SIZE"
    )
    hourly_card_update_heartbeat_seconds: int = Field(
        default=30, ge=10, le=300, validation_alias="HOURLY_CARD_UPDATE_HEARTBEAT_SECONDS"
    )
    hourly_card_update_lease_seconds: int = Field(
        default=90, ge=30, le=900, validation_alias="HOURLY_CARD_UPDATE_LEASE_SECONDS"
    )
    hourly_card_update_legacy_heartbeat_stale_seconds: int = Field(
        default=180, ge=30, le=3600, validation_alias="HOURLY_CARD_UPDATE_LEGACY_HEARTBEAT_STALE_SECONDS"
    )
    hourly_card_update_owner_marker_path: str = Field(
        default="/tmp/hourly-card-updater-owner", validation_alias="HOURLY_CARD_UPDATE_OWNER_MARKER_PATH"
    )
    teams_message_update_timeout_seconds: int = Field(
        default=25, ge=1, le=120, validation_alias="TEAMS_MESSAGE_UPDATE_TIMEOUT_SECONDS"
    )
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: DEFAULT_CORS_ALLOWED_ORIGINS.copy(),
        validation_alias="CORS_ALLOWED_ORIGINS",
        validate_default=True,
    )

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _parse_origins(cls, value: object) -> list[str]:
        """Parse comma-separated origins without introducing implicit values."""
        if isinstance(value, str):
            origins = value.split(",")
        elif isinstance(value, (list, tuple)):
            origins = value
        else:
            raise ValueError("CORS_ALLOWED_ORIGINS must be a comma-separated string or list")

        cleaned = [origin.strip() for origin in origins if isinstance(origin, str)]
        cleaned = [origin for origin in cleaned if origin]
        if "*" in cleaned:
            raise ValueError("CORS_ALLOWED_ORIGINS must not contain '*'")
        return cleaned


@lru_cache
def get_settings() -> Settings:
    return Settings()
