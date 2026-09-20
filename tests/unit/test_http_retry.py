"""The retry wrapper: classification, backoff and the fixtures mode guard."""

from __future__ import annotations

import random

import httpx
import pytest
import respx

from src.common.config import Settings
from src.common.errors import (
    ConfigError,
    RateLimitedError,
    UpstreamRejectedError,
    UpstreamUnavailableError,
)
from src.common.http import backoff_delay, request_with_retry

URL = "https://example.test/v1/thing"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        replay_fixtures=False,
        http_max_attempts=5,
        http_first_delay_seconds=0.0,
        http_max_delay_seconds=0.0,
        http_timeout_seconds=1.0,
    )


@respx.mock
async def test_retries_503_twice_then_succeeds(settings: Settings) -> None:
    """Spec section 8: 503, 503, 200 is one success over three attempts."""
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    response = await request_with_retry("GET", URL, settings=settings)
    assert response.json() == {"ok": True}
    assert route.call_count == 3


@respx.mock
@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_retryable_statuses_are_retried(settings: Settings, status: int) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(status))
    with pytest.raises(UpstreamUnavailableError):
        await request_with_retry("GET", URL, settings=settings)
    assert route.call_count == settings.http_max_attempts


@respx.mock
@pytest.mark.parametrize("status", [400, 403])
async def test_never_retried_statuses_fail_on_the_first_attempt(
    settings: Settings, status: int
) -> None:
    """A 400 or 403 will be identical next time. Retrying wastes the budget."""
    route = respx.get(URL).mock(return_value=httpx.Response(status))
    with pytest.raises(UpstreamRejectedError) as caught:
        await request_with_retry("GET", URL, settings=settings)
    assert caught.value.status_code == status
    assert route.call_count == 1


@respx.mock
async def test_429_raises_a_rate_limit_error(settings: Settings) -> None:
    respx.get(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "0"}))
    with pytest.raises(RateLimitedError):
        await request_with_retry("GET", URL, settings=settings)


@respx.mock
async def test_transport_errors_are_retried(settings: Settings) -> None:
    route = respx.get(URL).mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(UpstreamUnavailableError):
        await request_with_retry("GET", URL, settings=settings)
    assert route.call_count == settings.http_max_attempts


async def test_fixtures_mode_refuses_to_make_any_call() -> None:
    """Rule 5.4 is enforced here, not merely intended.

    The wrapper is the only outbound path in feature code, so refusing at
    this point makes an accidental network call in fixtures mode impossible
    rather than unlikely.
    """
    fixtures = Settings(environment="test", replay_fixtures=True)
    with pytest.raises(ConfigError) as caught:
        await request_with_retry("GET", URL, settings=fixtures)
    assert "fixtures mode" in str(caught.value)


def test_backoff_uses_full_jitter_within_the_cap() -> None:
    rng = random.Random(7)
    for attempt in range(1, 6):
        ceiling = min(32.0, 1.0 * 2 ** (attempt - 1))
        for _ in range(50):
            delay = backoff_delay(attempt, first_delay=1.0, max_delay=32.0, rng=rng)
            assert 0.0 <= delay <= ceiling


def test_backoff_is_capped_at_the_configured_maximum() -> None:
    rng = random.Random(1)
    delay = backoff_delay(20, first_delay=1.0, max_delay=32.0, rng=rng)
    assert delay <= 32.0
