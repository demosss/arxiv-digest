"""HTTP middleware: X-Request-ID и лог каждого запроса (без тел запросов)."""

import logging
import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import get_logger

log = get_logger("app.http")

REQUEST_ID_HEADER = "X-Request-ID"
# healthcheck дёргается каждые несколько секунд — на INFO он только шумит
_QUIET_PATHS = frozenset({"/api/health"})


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        path = request.url.path

        try:
            response = await call_next(request)
        except Exception:
            log.exception(
                "http_request_failed",
                method=request.method,
                path=path,
                status=500,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                user=getattr(request.state, "username", None),
            )
            raise

        level = (
            logging.DEBUG if path in _QUIET_PATHS and response.status_code < 400 else logging.INFO
        )
        log.log(
            level,
            "http_request",
            method=request.method,
            path=path,
            status=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            # username кладёт в request.state зависимость авторизации
            user=getattr(request.state, "username", None),
            client=request.client.host if request.client else None,
        )
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
