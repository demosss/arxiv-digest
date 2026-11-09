"""Единый формат ошибок API: {"detail": ..., "code": ...}."""

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

log = get_logger(__name__)

_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    429: "too_many_requests",
    503: "service_unavailable",
}


class RetryableError(Exception):
    """Временная ошибка (сеть, недоступный внешний сервис): фоновую задачу стоит повторить позже."""

    # база экспоненциальной паузы между повторами задачи; None — значение по умолчанию
    retry_backoff_base: int | None = None


class AppError(Exception):
    """Ошибка бизнес-логики с HTTP-статусом и машинным кодом."""

    def __init__(
        self, status_code: int, code: str, detail: Any, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.headers = headers


class NotFoundError(AppError):
    def __init__(self, detail: str = "Объект не найден") -> None:
        super().__init__(status.HTTP_404_NOT_FOUND, "not_found", detail)


class UnauthorizedError(AppError):
    def __init__(self, detail: str = "Требуется авторизация") -> None:
        super().__init__(
            status.HTTP_401_UNAUTHORIZED, "unauthorized", detail, {"WWW-Authenticate": "Bearer"}
        )


class ConflictError(AppError):
    def __init__(self, detail: str, code: str = "conflict") -> None:
        super().__init__(status.HTTP_409_CONFLICT, code, detail)


class BadRequestError(AppError):
    def __init__(self, detail: str, code: str = "bad_request") -> None:
        super().__init__(status.HTTP_400_BAD_REQUEST, code, detail)


class TooManyRequestsError(AppError):
    def __init__(self, detail: str, code: str = "too_many_requests") -> None:
        super().__init__(status.HTTP_429_TOO_MANY_REQUESTS, code, detail)


def _validation_item(err: Any) -> dict[str, Any]:
    # input и ctx не возвращаем: в них может оказаться присланный пароль
    item = {"loc": list(err.get("loc", ())), "msg": err.get("msg", ""), "type": err.get("type")}
    if err.get("type") == "invalid_query":
        # ошибка поискового запроса темы: позиция в нормализованной строке
        item["position"] = (err.get("ctx") or {}).get("position")
    return item


def _error(
    status_code: int, detail: Any, code: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": jsonable_encoder(detail), "code": code},
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return _error(exc.status_code, exc.detail, exc.code, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "error")
        headers = dict(exc.headers) if exc.headers else None
        return _error(exc.status_code, exc.detail, code, headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error(422, [_validation_item(err) for err in exc.errors()], "validation_error")

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        return _error(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Внутренняя ошибка сервера", "internal_error"
        )
