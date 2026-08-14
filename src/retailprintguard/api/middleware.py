"""Correlation ID, security headers and uniform API failure handling."""

from __future__ import annotations

import logging
import re
from time import monotonic
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from retailprintguard.api.repository import RepositoryUnavailable

CORRELATION_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


class CorrelationAndSecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        started = monotonic()
        supplied = request.headers.get("X-Correlation-ID", "")
        correlation_id = supplied if CORRELATION_RE.fullmatch(supplied) else str(uuid4())
        request.state.correlation_id = correlation_id
        try:
            response = await call_next(request)
        except RepositoryUnavailable:
            logging.getLogger("retailprintguard.api").warning(
                "control plane repository unavailable",
                extra={
                    "event": "api_request_failed",
                    "correlation_id": correlation_id,
                    "status": 503,
                    "details": {"method": request.method, "path": request.url.path},
                },
            )
            response = JSONResponse(
                status_code=503,
                content={
                    "error": "CONTROL_PLANE_UNAVAILABLE",
                    "message": "Il database non è temporaneamente disponibile.",
                    "correlation_id": correlation_id,
                },
            )
        except Exception:
            logging.getLogger("retailprintguard.api").exception(
                "Unhandled API error", extra={"correlation_id": correlation_id}
            )
            response = JSONResponse(
                status_code=500,
                content={
                    "error": "INTERNAL_ERROR",
                    "message": "Errore interno.",
                    "correlation_id": correlation_id,
                },
            )
        response.headers["X-Correlation-ID"] = correlation_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        )
        response.headers["Cache-Control"] = "no-store"
        logging.getLogger("retailprintguard.api").info(
            "API request completed",
            extra={
                "event": "api_request_completed",
                "correlation_id": correlation_id,
                "status": response.status_code,
                "details": {
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round((monotonic() - started) * 1000, 3),
                },
            },
        )
        return response
