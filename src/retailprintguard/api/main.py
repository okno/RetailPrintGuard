"""FastAPI application factory and production entry point."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from retailprintguard import __version__
from retailprintguard.api.auth import LoginThrottle, TokenService
from retailprintguard.api.middleware import CorrelationAndSecurityMiddleware
from retailprintguard.api.repository import ApiRepository, EmptyRepository
from retailprintguard.api.review_secret import DEFAULT_ENV_NAME, ReviewSecretVerifier
from retailprintguard.api.routes import ApiContext, create_router
from retailprintguard.common.config import Settings, load_settings
from retailprintguard.common.logging import configure_structured_logging


def create_app(
    *,
    repository: ApiRepository | None = None,
    jwt_secret: bytes | None = None,
    settings: Settings | None = None,
    review_password_hash: str | None = None,
) -> FastAPI:
    if jwt_secret is None:
        jwt_secret = b"test-only-secret-not-for-production-00000000000000000000"
    token_minutes = settings.api.access_token_minutes if settings else 30
    failed_limit = settings.api.failed_login_limit if settings else 5
    failed_delay = settings.api.failed_login_delay_seconds if settings else 2
    if review_password_hash is None and settings is not None:
        review_password_hash = os.environ.get(DEFAULT_ENV_NAME)
    context = ApiContext(
        repository or EmptyRepository(),
        TokenService(jwt_secret, lifetime_minutes=token_minutes),
        LoginThrottle(limit=failed_limit, delay_seconds=failed_delay),
        ReviewSecretVerifier(review_password_hash),
        LoginThrottle(limit=failed_limit, delay_seconds=failed_delay),
    )
    app = FastAPI(
        title="RetailPrintGuard API",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(CorrelationAndSecurityMiddleware)
    if settings and settings.api.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.api.allowed_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH"],
            allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
        )
    app.include_router(create_router(context))
    return app


def _read_secret(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"JWT secret must be a regular non-symlink file: {path}")
    if path.stat().st_size > 4096:
        raise RuntimeError("JWT secret file is too large")
    secret = path.read_bytes().strip()
    if len(secret) < 32:
        raise RuntimeError("JWT secret must contain at least 32 bytes")
    return secret


def build_production_repository(settings: Settings) -> ApiRepository:
    """Load the SQLAlchemy adapter lazily so the API never reaches proxy code."""

    from retailprintguard.db.repository import create_api_repository

    return create_api_repository(settings)


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RetailPrintGuard control-plane API")
    parser.add_argument(
        "--config", default=os.environ.get("RPG_CONFIG", "/etc/retailprintguard/config.yaml")
    )
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    runtime = configure_structured_logging("api")
    try:
        settings = load_settings(args.config)
        secret_path = Path(
            os.environ.get("RPG_JWT_SECRET_FILE", "/etc/retailprintguard/jwt.secret")
        )
        repository = build_production_repository(settings)
        app = create_app(
            repository=repository,
            jwt_secret=_read_secret(secret_path),
            settings=settings,
        )
        uvicorn.run(
            app,
            host=str(settings.api.bind_host),
            port=settings.api.bind_port,
            access_log=False,
            server_header=False,
            log_config=None,
        )
    finally:
        runtime.stop()
    return 0


app: Any = create_app()


if __name__ == "__main__":
    raise SystemExit(cli())
