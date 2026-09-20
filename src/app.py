"""FastAPI application.

Startup resolves every client once and warms the OIDC certificate cache, so
no request pays for a JWKS fetch or a credential lookup.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.common.config import Settings, get_settings
from src.common.db import dispose_engine
from src.common.deps import Dependencies, build_dependencies
from src.common.errors import (
    ApAutopilotError,
    AuthenticationError,
    ConfigError,
    ExtractionSchemaError,
    NotFoundError,
    PayloadTooLargeError,
    TransientError,
    UnsupportedMediaTypeError,
)
from src.common.logging import configure_logging, get_logger
from src.common.routes_ops import router as ops_router
from src.extraction.routes import router as extraction_router
from src.ingestion.routes import router as ingestion_router

log = get_logger(__name__)

# Typed errors to HTTP status. Anything not listed is a 500, which is the
# right answer for a failure nobody classified.
_STATUS_BY_ERROR: tuple[tuple[type[ApAutopilotError], int], ...] = (
    (AuthenticationError, 401),
    (NotFoundError, 404),
    (PayloadTooLargeError, 413),
    (UnsupportedMediaTypeError, 415),
    (ExtractionSchemaError, 422),
    (ConfigError, 500),
    (TransientError, 503),
)


def _status_for(error: ApAutopilotError) -> int:
    for error_type, status in _STATUS_BY_ERROR:
        if isinstance(error, error_type):
            return status
    return 500


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Resolve dependencies and warm caches before serving."""
    settings: Settings = app.state.settings
    configure_logging(settings.environment)
    deps: Dependencies = build_dependencies(settings)
    app.state.deps = deps
    try:
        await deps.verifier.prime()
    except Exception as exc:
        # A cold instance that cannot reach the certificate endpoint will
        # reject every request with 401 until it can. Pub/Sub retries, so
        # the messages are not lost, but this needs to be loud.
        log.error("startup.oidc_prime_failed", error=str(exc))
    log.info(
        "startup.ready",
        environment=settings.environment,
        fixtures_mode=settings.replay_fixtures,
    )
    try:
        yield
    finally:
        closer = getattr(deps.verifier, "aclose", None)
        if closer is not None:
            await closer()
        await dispose_engine()
        log.info("shutdown.complete")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application."""
    cfg = settings or get_settings()
    configure_logging(cfg.environment)

    app = FastAPI(
        title="ap-autopilot",
        version="0.1.0",
        summary="Accounts payable ingestion and extraction",
        lifespan=lifespan,
    )
    app.state.settings = cfg

    @app.exception_handler(ApAutopilotError)
    async def handle_known_error(
        request: Request, exc: ApAutopilotError
    ) -> JSONResponse:
        status = _status_for(exc)
        body: dict[str, object] = {"error": exc.code, "message": exc.message}
        if exc.detail:
            body["detail"] = exc.detail
        if isinstance(exc, ExtractionSchemaError):
            body["pointer"] = exc.pointer
        log_method = log.warning if status < 500 else log.error
        log_method(
            "request.failed",
            path=request.url.path,
            status=status,
            error=exc.code,
            detail=exc.detail,
        )
        return JSONResponse(status_code=status, content=body)

    app.include_router(ingestion_router)
    app.include_router(extraction_router)
    app.include_router(ops_router)
    return app


app = create_app()
