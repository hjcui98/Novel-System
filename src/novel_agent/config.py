"""Process configuration loaded from environment variables and an optional local .env file."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnvironment(StrEnum):
    """Supported runtime configuration profiles."""

    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class AppSettings(BaseSettings):
    """Non-secret process settings.

    Environment variables override values from ``.env``. Model endpoints and
    credentials deliberately do not exist in the Stage 0 bootstrap contract.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="NOVEL_AGENT_",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: AppEnvironment = AppEnvironment.DEVELOPMENT
    log_level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
