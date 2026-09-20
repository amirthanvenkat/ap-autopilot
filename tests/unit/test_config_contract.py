"""Configuration contracts.

Rule 5.5 says .env.example lists every variable. A rule that depends on
someone remembering is a rule that decays, so it is checked here instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.common.config import Settings
from src.common.errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"


def documented_keys() -> set[str]:
    """Variable names declared in .env.example."""
    pattern = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)
    return set(pattern.findall(ENV_EXAMPLE.read_text("utf-8")))


def test_every_setting_appears_in_the_env_example() -> None:
    expected = {name.upper() for name in Settings.model_fields}
    missing = expected - documented_keys()
    assert not missing, f".env.example is missing: {sorted(missing)}"


def test_the_env_example_declares_nothing_that_is_not_a_setting() -> None:
    extra = documented_keys() - {name.upper() for name in Settings.model_fields}
    assert not extra, f".env.example declares unknown settings: {sorted(extra)}"


def test_the_env_example_carries_no_real_secret() -> None:
    """Rule 5.5. Placeholders only."""
    text = ENV_EXAMPLE.read_text("utf-8")
    assert "neon.tech/ap_autopilot" in text
    for line in text.splitlines():
        if line.startswith("OIDC_DEV_TOKEN="):
            assert line.endswith("change-me-in-local-only")


def test_fixtures_mode_needs_no_cloud_settings() -> None:
    """The demo must start with nothing configured."""
    Settings(replay_fixtures=True).require_live()


def test_live_mode_refuses_to_start_half_configured() -> None:
    """Failing at startup beats failing on the first invoice."""
    with pytest.raises(ConfigError) as caught:
        Settings(replay_fixtures=False).require_live()
    detail = str(caught.value)
    for name in (
        "GCP_PROJECT_ID",
        "GCS_BUCKET",
        "DOCAI_PROCESSOR_ID",
        "OIDC_AUDIENCE",
        "OIDC_SERVICE_ACCOUNT",
    ):
        assert name in detail


def test_document_ai_path_is_built_from_project_and_processor() -> None:
    settings = Settings(
        gcp_project_id="proj",
        gcp_location="asia-southeast1",
        docai_processor_id="abc123",
    )
    assert settings.docai_processor_path == (
        "projects/proj/locations/asia-southeast1/processors/abc123"
    )
    assert settings.docai_endpoint == (
        "https://asia-southeast1-documentai.googleapis.com"
    )


def test_document_ai_path_raises_when_unconfigured() -> None:
    with pytest.raises(ConfigError):
        _ = Settings().docai_processor_path


def test_the_publish_timeout_is_well_inside_the_acknowledgement_budget() -> None:
    """The publish is the only network call on the acceptance path."""
    assert Settings().publish_timeout_seconds <= 0.5


def test_retry_policy_matches_the_spec() -> None:
    settings = Settings()
    assert settings.http_max_attempts == 5
    assert settings.http_first_delay_seconds == 1.0
    assert settings.http_max_delay_seconds == 32.0


def test_upload_cap_is_twenty_megabytes() -> None:
    assert Settings().max_upload_bytes == 20 * 1024 * 1024
