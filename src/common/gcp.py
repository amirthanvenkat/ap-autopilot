"""Google credentials for outbound calls.

Only credential minting uses a Google library. Every actual request goes out
through common.http.request_with_retry as the REST API, because rule 5.1
gives that wrapper ownership of timeouts, backoff and retry classification,
and the vendor client libraries would route around it.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

import google.auth
import google.auth.transport
import httpx
from google.auth.credentials import Credentials

from src.common.errors import ConfigError
from src.common.logging import get_logger

log = get_logger(__name__)


class _HttpxAuthResponse(google.auth.transport.Response):
    """Adapts an httpx response to what google-auth expects."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    @property
    def status(self) -> int:
        return self._response.status_code

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._response.headers)

    @property
    def data(self) -> bytes:
        return self._response.content


class HttpxAuthRequest(google.auth.transport.Request):
    """Token refresh transport backed by httpx.

    This is the one place a bare HTTP client is correct. Rule 5.1 routes
    outbound feature calls through request_with_retry, and that wrapper
    needs a bearer token to make its call, so minting the token cannot go
    through it without a cycle. Using httpx here also avoids pulling in a
    second synchronous HTTP stack purely for credential refresh.
    """

    def __init__(self, timeout: float = 20.0) -> None:
        self._timeout = timeout

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: object,
    ) -> _HttpxAuthResponse:
        del kwargs
        with httpx.Client(timeout=timeout or self._timeout) as client:
            response = client.request(method, url, content=body, headers=headers)
        return _HttpxAuthResponse(response)


CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
DEFAULT_SCOPES = (CLOUD_PLATFORM_SCOPE, GMAIL_READONLY_SCOPE)


class TokenProvider(Protocol):
    """Supplies a bearer token for outbound Google API calls."""

    async def token(self) -> str: ...


class ApplicationDefaultTokenProvider:
    """Application default credentials, refreshed off the event loop.

    google-auth refreshes synchronously, so the refresh runs in a worker
    thread. A blocking refresh on the event loop would stall every other
    request on the instance.
    """

    def __init__(self, scopes: tuple[str, ...] = DEFAULT_SCOPES) -> None:
        self._scopes = list(scopes)
        self._credentials: Credentials | None = None
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        async with self._lock:
            if self._credentials is None:
                self._credentials = await asyncio.to_thread(self._load)
            credentials = self._credentials
            if not credentials.valid:
                await asyncio.to_thread(self._refresh, credentials)
        token = getattr(credentials, "token", None)
        if not token:
            raise ConfigError("Application default credentials produced no token")
        return str(token)

    def _load(self) -> Credentials:
        try:
            credentials, _project = google.auth.default(scopes=self._scopes)
        except google.auth.exceptions.DefaultCredentialsError as exc:
            raise ConfigError(
                "No application default credentials are available",
                detail=str(exc),
            ) from exc
        return credentials

    @staticmethod
    def _refresh(credentials: Credentials) -> None:
        credentials.refresh(HttpxAuthRequest())


class StaticTokenProvider:
    """A fixed token. Used by tests so no credential lookup happens."""

    def __init__(self, value: str = "test-token") -> None:
        self._value = value

    async def token(self) -> str:
        return self._value


async def auth_headers(provider: TokenProvider) -> dict[str, str]:
    """Bearer header for an outbound Google API call."""
    return {"Authorization": f"Bearer {await provider.token()}"}
