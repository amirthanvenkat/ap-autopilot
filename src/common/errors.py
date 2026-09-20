"""Typed error taxonomy.

The permanent/transient split is the load bearing distinction in this
codebase. A worker retries a transient failure and records a permanent one
without retrying, so every raised error must sit on one side of that line.
"""

from __future__ import annotations

# Status codes the retry wrapper treats as worth another attempt.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Status codes that must never be retried, however many attempts remain.
NEVER_RETRY_STATUS = frozenset({400, 401, 403, 404, 409, 413, 415, 422})


class ApAutopilotError(Exception):
    """Base for every error raised by this codebase."""

    code: str = "AP_ERROR"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:
        if self.detail:
            return f"{self.message}: {self.detail}"
        return self.message


class ConfigError(ApAutopilotError):
    """A required setting is missing or malformed."""

    code = "CONFIG_ERROR"


class TransientError(ApAutopilotError):
    """The operation may succeed if attempted again."""

    code = "TRANSIENT_ERROR"


class PermanentError(ApAutopilotError):
    """The operation will fail identically on every attempt."""

    code = "PERMANENT_ERROR"


class UpstreamUnavailableError(TransientError):
    """An upstream returned a retryable status or failed to respond."""

    code = "UPSTREAM_UNAVAILABLE"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.status_code = status_code


class RateLimitedError(TransientError):
    """An upstream applied rate limiting."""

    code = "RATE_LIMITED"

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.retry_after = retry_after


class UpstreamRejectedError(PermanentError):
    """An upstream returned a status that will not change on retry."""

    code = "UPSTREAM_REJECTED"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.status_code = status_code


class AuthenticationError(PermanentError):
    """An inbound request carried no valid credential."""

    code = "AUTHENTICATION_ERROR"


class UnsupportedMediaTypeError(PermanentError):
    """An uploaded file is not an accepted type."""

    code = "UNSUPPORTED_MEDIA_TYPE"


class PayloadTooLargeError(PermanentError):
    """An uploaded file exceeds the size cap."""

    code = "PAYLOAD_TOO_LARGE"


class NotFoundError(PermanentError):
    """A requested resource does not exist."""

    code = "NOT_FOUND"


class ExtractionSchemaError(PermanentError):
    """An extraction payload failed schema validation.

    Carries the JSON Pointer of the offending node so the job row records
    exactly where the payload broke.
    """

    code = "EXTRACTION_SCHEMA_ERROR"

    def __init__(
        self, message: str, *, pointer: str, detail: str | None = None
    ) -> None:
        super().__init__(message, detail=detail)
        self.pointer = pointer

    def __str__(self) -> str:
        base = f"{self.message} at {self.pointer}"
        if self.detail:
            return f"{base}: {self.detail}"
        return base


class FixtureMissingError(PermanentError):
    """Fixtures mode was asked for a response that is not on disk."""

    code = "FIXTURE_MISSING"


class GmailHistoryExpiredError(PermanentError):
    """The stored Gmail history id is too old to diff from.

    Permanent for the stored cursor, but recoverable by full resync rather
    than by retrying the same history range.
    """

    code = "GMAIL_HISTORY_EXPIRED"


def is_transient(error: BaseException) -> bool:
    """Classify an error for the worker's retry decision.

    Anything not explicitly transient is treated as permanent. Retrying an
    unknown failure five times buys nothing and hides the cause.
    """
    return isinstance(error, TransientError)
