"""Единая настройка логирования для API, Celery worker и Celery beat.

structlog поверх stdlib logging: записи и из structlog, и из сторонних библиотек
проходят через один форматтер, получают поле `service` и маскирование секретов.
"""

import logging
import logging.handlers
import os
import re
import sys
import warnings
from collections.abc import Iterable
from typing import Any

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from app.core.config import get_settings

REDACTED = "***"
LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 5
# Короткие значения не вычищаем по содержимому: иначе пароль "admin" замаскирует и логин.
MIN_SECRET_VALUE_LENGTH = 8

SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "current_password",
        "old_password",
        "new_password",
        "password_hash",
        "admin_password",
        "postgres_password",
        "rabbitmq_password",
        "smtp_password",
        "llm_api_key",
        "api_key",
        "secret_key",
        "secret",
        "token",
        "access_token",
        "jwt",
        "authorization",
    }
)
_SENSITIVE_SUFFIXES = ("password", "_secret", "_token", "api_key")
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


def is_sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return lowered in SENSITIVE_KEYS or lowered.endswith(_SENSITIVE_SUFFIXES)


class SecretMasker:
    """Процессор structlog: маскирует поля по именам и известные значения секретов в строках."""

    def __init__(self, secret_values: Iterable[str] = ()) -> None:
        unique = {v for v in secret_values if len(v) >= MIN_SECRET_VALUE_LENGTH}
        self._values = tuple(sorted(unique, key=len, reverse=True))

    def scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self._values:
                if secret in value:
                    value = value.replace(secret, REDACTED)
            return _BEARER_RE.sub(rf"\1{REDACTED}", value)
        if isinstance(value, dict):
            return {k: REDACTED if is_sensitive_key(k) else self.scrub(v) for k, v in value.items()}
        if isinstance(value, list | tuple | set | frozenset):
            return [self.scrub(v) for v in value]
        return value

    def __call__(self, logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
        scrubbed: EventDict = self.scrub(event_dict)
        return scrubbed


def _add_service(service: str) -> Processor:
    def processor(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service)
        return event_dict

    return processor


def _drop_color_message(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """uvicorn дублирует сообщение с ANSI-цветами в extra — в логах оно не нужно."""
    event_dict.pop("color_message", None)
    return event_dict


def shared_processors(service: str) -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_service(service),
        structlog.processors.StackInfoRenderer(),
    ]


def build_formatter(service: str) -> structlog.stdlib.ProcessorFormatter:
    settings = get_settings()
    renderer: Processor
    if settings.log_format == "json":
        renderer = structlog.processors.JSONRenderer(ensure_ascii=False, default=str)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[*shared_processors(service), structlog.stdlib.ExtraAdder()],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _drop_color_message,
            # traceback превращается в строку до маскирования, чтобы секреты вычищались и из него
            structlog.processors.format_exc_info,
            SecretMasker(settings.secret_values()),
            renderer,
        ],
    )


def configure_logging(service: str | None = None) -> None:
    """Настраивает structlog и корневой логгер. Безопасно вызывать повторно."""
    settings = get_settings()
    service = service or os.environ.get("SERVICE_NAME", "api")
    warnings.filterwarnings("ignore", message="Remove `format_exc_info`")

    structlog.configure(
        processors=[
            *shared_processors(service),
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    formatter = build_formatter(service)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    file_error: str | None = None
    try:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                settings.logs_dir / f"{service}.log",
                maxBytes=LOG_FILE_MAX_BYTES,
                backupCount=LOG_FILE_BACKUP_COUNT,
                encoding="utf-8",
            )
        )
    except OSError as exc:
        file_error = str(exc)

    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
        old.close()
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # Сторонние логгеры пишут через корневой: единый формат вместо собственных хендлеров.
    for name in ("uvicorn", "uvicorn.error", "celery", "celery.task", "celery.beat", "alembic"):
        lib_logger = logging.getLogger(name)
        lib_logger.handlers.clear()
        lib_logger.propagate = True
    # HTTP-запросы логирует наш middleware
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    for name in ("httpx", "httpcore", "amqp", "kombu", "fontTools", "weasyprint", "openai"):
        logging.getLogger(name).setLevel(max(root.level, logging.WARNING))

    if file_error:
        get_logger(__name__).warning("log_file_unavailable", error=file_error)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
