"""Settings, including the validation that fails fast in production."""

from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> the repository root.
PROJECT_ROOT = Path(__file__).resolve().parents[3]


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
    log_json: bool | None = None

    # Attachment bytes live in S3-compatible object storage, never in PostgreSQL
    # (Decision 15). Credentials are optional here so a deployment can use boto3's own
    # chain (instance role, AWS_ACCESS_KEY_ID, ~/.aws/credentials) instead.
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_bucket: str = "soccom-attachments"
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_use_path_style: bool = True

    attachment_max_bytes: int = 10 * 1024 * 1024
    attachment_message_limit: int = 4
    attachment_account_quota_bytes: int = 200 * 1024 * 1024
    attachment_upload_ttl_seconds: int = 900

    # Uploads are scanned before they can be downloaded. Leave unset to run the structural
    # validator only (type, size, and magic-byte checks) outside production.
    clamav_host: str | None = None
    clamav_port: int = 3310

    # The working directory and the repository root are both consulted, so starting uvicorn
    # from `backend/` finds the same `.env` as starting it from the root. Real environment
    # variables always win over either file, which is how a deployment supplies its settings.
    model_config = SettingsConfigDict(
        env_file=(".env", str(PROJECT_ROOT / ".env")), extra="ignore"
    )

    @property
    def json_logs(self) -> bool:
        if self.log_json is not None:
            return self.log_json
        return self.app_env != "development"

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
            if not (self.s3_bucket and self.s3_endpoint_url):
                raise ValueError("S3_BUCKET and S3_ENDPOINT_URL are required in production")
            if not self.clamav_host:
                raise ValueError(
                    "CLAMAV_HOST is required in production: an upload must be scanned before "
                    "it can be downloaded"
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
