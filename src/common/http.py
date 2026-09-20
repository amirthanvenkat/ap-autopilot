"""The only outbound HTTP path in this codebase.

Rule 5.1: feature code never calls httpx directly. This wrapper owns
timeouts, backoff with full jitter, retry classification, and the per host
concurrency and rate limits that Xero requires in a later module.

It also refuses to make any call at all when REPLAY_FIXTURES is set, which
is what makes rule 5.4 enforceable rather than aspirational.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit

import httpx

from src.common.config import Settings, get_settings
from src.common.errors import (
    NEVER_RETRY_STATUS,
    RETRYABLE_STATUS,
    ConfigError,
    RateLimitedError,
    UpstreamRejectedError,
    UpstreamUnavailableError,
)
from src.common.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class HostLimits:
    """Concurrency and rate ceilings for one host."""

    max_concurrent: int = 10
    calls_per_minute: int | None = None


# Xero allows 60 calls per minute and 5 concurrent. Declared here so the
# limit lives with the wrapper that enforces it.
HOST_LIMITS: dict[str, HostLimits] = {
    "api.xero.com": HostLimits(max_concurrent=5, calls_per_minute=60),
}
_DEFAULT_LIMITS = HostLimits()


@dataclass
class _HostGate:
    """Concurrency semaphore and sliding window for one host."""

    limits: HostLimits
    semaphore: asyncio.Semaphore = field(init=False)
    window: list[float] = field(default_factory=list, init=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        self.semaphore = asyncio.Semaphore(self.limits.max_concurrent)

    async def acquire_slot(self) -> None:
        """Block until the per minute allowance has room."""
        if self.limits.calls_per_minute is None:
            return
        async with self.lock:
            while True:
                now = time.monotonic()
                self.window[:] = [t for t in self.window if now - t < 60.0]
                if len(self.window) < self.limits.calls_per_minute:
                    self.window.append(now)
                    return
                sleep_for = 60.0 - (now - self.window[0])
                await asyncio.sleep(max(sleep_for, 0.01))


_gates: dict[str, _HostGate] = {}


def _gate_for(host: str) -> _HostGate:
    gate = _gates.get(host)
    if gate is None:
        gate = _HostGate(HOST_LIMITS.get(host, _DEFAULT_LIMITS))
        _gates[host] = gate
    return gate


def backoff_delay(
    attempt: int,
    *,
    first_delay: float,
    max_delay: float,
    rng: random.Random | None = None,
) -> float:
    """Exponential backoff with full jitter.

    Full jitter, not equal jitter. A herd of Cloud Run instances retrying the
    same Document AI outage should spread across the whole window rather than
    cluster at its midpoint.
    """
    ceiling = min(max_delay, first_delay * (2 ** max(attempt - 1, 0)))
    source = rng or random
    return source.uniform(0.0, ceiling)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


async def request_with_retry(
    method: str,
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    settings: Settings | None = None,
    timeout: float | None = None,
    max_attempts: int | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Perform one outbound request, retrying transient failures.

    Raises UpstreamUnavailableError or RateLimitedError when the failure may
    succeed on another attempt, and UpstreamRejectedError when it will not.
    """
    cfg = settings or get_settings()
    if cfg.replay_fixtures:
        raise ConfigError(
            "Outbound HTTP attempted in fixtures mode",
            detail=f"{method} {url}",
        )

    attempts = max_attempts or cfg.http_max_attempts
    host = urlsplit(url).hostname or ""
    gate = _gate_for(host)
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(cfg.http_timeout_seconds))

    try:
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            await gate.acquire_slot()
            try:
                async with gate.semaphore:
                    response = await http.request(
                        method,
                        url,
                        timeout=timeout or cfg.http_timeout_seconds,
                        **kwargs,
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                log.warning(
                    "http.transport_error",
                    method=method,
                    url=url,
                    attempt=attempt,
                    error=str(exc),
                )
            else:
                status = response.status_code
                if status < 400:
                    return response
                if status in NEVER_RETRY_STATUS:
                    raise UpstreamRejectedError(
                        f"{method} {url} rejected",
                        status_code=status,
                        detail=response.text[:2000],
                    )
                if status not in RETRYABLE_STATUS:
                    raise UpstreamRejectedError(
                        f"{method} {url} returned an unhandled status",
                        status_code=status,
                        detail=response.text[:2000],
                    )
                retry_after = _retry_after_seconds(response)
                last_error = (
                    RateLimitedError(
                        f"{method} {url} rate limited", retry_after=retry_after
                    )
                    if status == 429
                    else UpstreamUnavailableError(
                        f"{method} {url} unavailable", status_code=status
                    )
                )
                log.warning(
                    "http.retryable_status",
                    method=method,
                    url=url,
                    attempt=attempt,
                    status=status,
                )

            if attempt == attempts:
                break
            delay = backoff_delay(
                attempt,
                first_delay=cfg.http_first_delay_seconds,
                max_delay=cfg.http_max_delay_seconds,
            )
            if isinstance(last_error, RateLimitedError) and last_error.retry_after:
                delay = max(delay, last_error.retry_after)
            await asyncio.sleep(delay)

        if isinstance(last_error, RateLimitedError):
            raise last_error
        raise UpstreamUnavailableError(
            f"{method} {url} failed after {attempts} attempts",
            detail=str(last_error) if last_error else None,
        )
    finally:
        if owns_client:
            await http.aclose()


class RetryingClient:
    """A reusable client that applies the retry policy to every request."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._settings.http_timeout_seconds)
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        timeout: float | None = None,
        max_attempts: int | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        return await request_with_retry(
            method,
            url,
            client=self._client,
            settings=self._settings,
            timeout=timeout,
            max_attempts=max_attempts,
            **kwargs,
        )
