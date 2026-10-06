from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "postgresql+psycopg://soccom:soccom@localhost:5433/soccom"
    cookie_secure: bool = False
    app_base_url: str = "http://localhost:8000"
    smtp_host: str | None = None
    smtp_port: int = 1025
    smtp_from: str = "accounts@soccom.local"
    smtp_starttls: bool = False
    smtp_username: str | None = None
    smtp_password: str | None = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        if self.app_env == "production":
            if not self.cookie_secure:
                raise ValueError("COOKIE_SECURE must be enabled in production")
            if not self.smtp_host:
                raise ValueError("SMTP_HOST is required in production")
            if not self.smtp_starttls:
                raise ValueError("SMTP_STARTTLS must be enabled in production")
            if not self.app_base_url.startswith("https://"):
                raise ValueError("APP_BASE_URL must use HTTPS in production")
            if not self.database_url.startswith("postgresql+psycopg://"):
                raise ValueError("DATABASE_URL must use PostgreSQL in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
