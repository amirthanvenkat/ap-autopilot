"""Inbound OIDC verification for the two Pub/Sub push endpoints.

Google's signing certificates are cached in process and refreshed on a
background timer. Nothing in the request path fetches them, because a JWKS
fetch inside the handler would put a network round trip on the critical path
and make the latency budget depend on Google's availability.

Fixtures mode verifies a shared secret instead. Rule 5.4 means the public
demo must run with no external call, and a push endpoint that stops checking
credentials in fixtures mode would make the 401 test meaningless.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Protocol

from google.auth import jwt as google_jwt

from src.common.config import Settings, get_settings
from src.common.errors import AuthenticationError
from src.common.http import request_with_retry
from src.common.logging import get_logger

log = get_logger(__name__)

GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
VALID_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
_REFRESH_INTERVAL_SECONDS = 3600.0
_STALE_AFTER_SECONDS = 86400.0


@dataclass(frozen=True)
class TokenClaims:
    """The claims this codebase cares about."""

    subject: str
    email: str
    audience: str
    issuer: str
    expires_at: int


class TokenVerifier(Protocol):
    """Verifies the bearer credential on an inbound push request."""

    async def verify(self, authorization_header: str | None) -> TokenClaims: ...

    async def prime(self) -> None: ...


def _bearer(authorization_header: str | None) -> str:
    if not authorization_header:
        raise AuthenticationError("Missing Authorization header")
    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthenticationError("Authorization header is not a bearer token")
    return token.strip()


class GoogleOidcVerifier:
    """Verifies a Google signed OIDC token against cached certificates."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._certs: dict[str, str] = {}
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    async def prime(self) -> None:
        """Fetch certificates once and start the refresh loop.

        Called from application startup, never from a request handler.
        """
        await self._refresh()
        if self._task is None:
            self._task = asyncio.create_task(self._refresh_loop())

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(_REFRESH_INTERVAL_SECONDS)
                await self._refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("oidc.refresh_failed", error=str(exc))

    async def _refresh(self) -> None:
        response = await request_with_retry(
            "GET", GOOGLE_CERTS_URL, settings=self._settings
        )
        certs: dict[str, str] = response.json()
        async with self._lock:
            self._certs = certs
            self._fetched_at = time.monotonic()
        log.info("oidc.certs_refreshed", count=len(certs))

    async def verify(self, authorization_header: str | None) -> TokenClaims:
        token = _bearer(authorization_header)
        if not self._certs:
            raise AuthenticationError(
                "Signing certificates are not loaded",
                detail="prime() must run at startup",
            )
        age = time.monotonic() - self._fetched_at
        if age > _STALE_AFTER_SECONDS:
            log.warning("oidc.certs_stale", age_seconds=int(age))

        try:
            payload: dict[str, Any] = google_jwt.decode(
                token,
                certs=self._certs,
                audience=self._settings.oidc_audience or None,
            )
        except ValueError as exc:
            raise AuthenticationError(
                "Token failed verification", detail=str(exc)
            ) from exc

        return _validate_claims(payload, self._settings)


class SharedSecretVerifier:
    """Fixtures mode verifier. Compares a configured token, nothing more."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def prime(self) -> None:
        return None

    async def verify(self, authorization_header: str | None) -> TokenClaims:
        token = _bearer(authorization_header)
        expected = self._settings.oidc_dev_token
        if not expected:
            raise AuthenticationError("Fixtures mode has no OIDC_DEV_TOKEN configured")
        # Constant time comparison is overkill for a local demo token, but it
        # costs nothing and the habit belongs in a credential check.
        if not _constant_time_equal(token, expected):
            raise AuthenticationError("Bearer token does not match")
        return TokenClaims(
            subject="fixtures",
            email=self._settings.oidc_service_account or "fixtures@example.test",
            audience=self._settings.oidc_audience or "fixtures",
            issuer="fixtures",
            expires_at=0,
        )


def _constant_time_equal(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    result = 0
    for a, b in zip(left, right, strict=True):
        result |= ord(a) ^ ord(b)
    return result == 0


def _validate_claims(payload: dict[str, Any], settings: Settings) -> TokenClaims:
    """Check issuer, audience and calling service account."""
    issuer = str(payload.get("iss", ""))
    if issuer not in VALID_ISSUERS:
        raise AuthenticationError("Unexpected token issuer", detail=issuer)

    audience = str(payload.get("aud", ""))
    if settings.oidc_audience and audience != settings.oidc_audience:
        raise AuthenticationError("Unexpected token audience", detail=audience)

    email = str(payload.get("email", ""))
    if settings.oidc_service_account and email != settings.oidc_service_account:
        raise AuthenticationError("Unexpected calling service account", detail=email)

    if settings.oidc_service_account and not payload.get("email_verified", False):
        raise AuthenticationError("Service account email is not verified")

    return TokenClaims(
        subject=str(payload.get("sub", "")),
        email=email,
        audience=audience,
        issuer=issuer,
        expires_at=int(payload.get("exp", 0)),
    )


def build_verifier(settings: Settings | None = None) -> TokenVerifier:
    """Choose a verifier by configuration, never by branching at the call site."""
    cfg = settings or get_settings()
    if cfg.replay_fixtures:
        return SharedSecretVerifier(cfg)
    return GoogleOidcVerifier(cfg)
