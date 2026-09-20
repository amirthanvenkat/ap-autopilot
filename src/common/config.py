"""Application settings.

Every value comes from the environment. Adding a field here means adding a
line to .env.example in the same commit.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.common.errors import ConfigError

_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: str = Field(default="local")

    # Fixtures mode. Rule 5.4: true must give a full working demo with no
    # external call of any kind.
    replay_fixtures: bool = Field(default=False)
    fixtures_dir: Path = Field(default=_REPO_ROOT / "fixtures")

    # Database. Use the Neon pooled endpoint: Cloud Run scales out and the
    # free tier connection cap is low.
    database_url: str = Field(default="postgresql+psycopg://localhost/ap_autopilot")
    db_pool_size: int = Field(default=2)
    db_max_overflow: int = Field(default=2)
    db_statement_timeout_ms: int = Field(default=5_000)

    # Google Cloud.
    gcp_project_id: str = Field(default="")
    gcp_location: str = Field(default="asia-southeast1")
    gcs_bucket: str = Field(default="")
    docai_processor_id: str = Field(default="")

    # Pub/Sub topics.
    pubsub_work_topic: str = Field(default="ingestion-work")
    pubsub_deadletter_subscription: str = Field(default="ingestion-deadletter-sub")

    # Inbound OIDC verification for the two push endpoints.
    oidc_audience: str = Field(default="")
    oidc_service_account: str = Field(default="")
    oidc_issuer: str = Field(default="https://accounts.google.com")
    # Fixtures mode has no network, so it verifies a shared secret
    # instead of a Google signed token. The 401 path stays real.
    oidc_dev_token: str = Field(default="")

    # Gmail.
    gmail_user_id: str = Field(default="me")
    gmail_label: str = Field(default="ap-inbox")
    gmail_min_attachment_bytes: int = Field(default=8_192)

    # Upload limits. Spec 01 section 4.
    max_upload_bytes: int = Field(default=20 * 1024 * 1024)

    # Outbox and worker timing.
    outbox_lease_seconds: int = Field(default=300)
    outbox_max_attempts: int = Field(default=5)
    job_timeout_seconds: int = Field(default=900)

    # Outbound HTTP. The publish timeout is deliberately short: it sits
    # outside the acceptance transaction and the sweeper is the safety net.
    http_timeout_seconds: float = Field(default=10.0)
    http_max_attempts: int = Field(default=5)
    http_first_delay_seconds: float = Field(default=1.0)
    http_max_delay_seconds: float = Field(default=32.0)
    publish_timeout_seconds: float = Field(default=0.25)

    @field_validator("fixtures_dir")
    @classmethod
    def _resolve_fixtures_dir(cls, value: Path) -> Path:
        return value if value.is_absolute() else (_REPO_ROOT / value).resolve()

    @property
    def docai_endpoint(self) -> str:
        """Regional Document AI host. The processor is region pinned."""
        return f"https://{self.gcp_location}-documentai.googleapis.com"

    @property
    def docai_processor_path(self) -> str:
        if not (self.gcp_project_id and self.docai_processor_id):
            raise ConfigError(
                "Document AI is not configured",
                detail="set GCP_PROJECT_ID and DOCAI_PROCESSOR_ID",
            )
        return (
            f"projects/{self.gcp_project_id}"
            f"/locations/{self.gcp_location}"
            f"/processors/{self.docai_processor_id}"
        )

    def require_live(self) -> None:
        """Fail loudly when live mode is missing the settings it needs."""
        if self.replay_fixtures:
            return
        missing = [
            name
            for name, value in (
                ("GCP_PROJECT_ID", self.gcp_project_id),
                ("GCS_BUCKET", self.gcs_bucket),
                ("DOCAI_PROCESSOR_ID", self.docai_processor_id),
                ("OIDC_AUDIENCE", self.oidc_audience),
                ("OIDC_SERVICE_ACCOUNT", self.oidc_service_account),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "Live mode is missing required settings",
                detail=", ".join(missing),
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process wide settings singleton."""
    return Settings()
