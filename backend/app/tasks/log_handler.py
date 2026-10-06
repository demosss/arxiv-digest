"""Logging handler, который пишет записи задачи в таблицу task_logs.

Записи копятся в буфере и вставляются батчами через отдельное соединение,
поэтому ошибка логирования не роняет задачу и не затрагивает её транзакцию.
"""

import json
import logging
import sys
import time
import traceback
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert

from app.core.config import get_settings
from app.core.db import get_sync_engine
from app.core.logging import SecretMasker
from app.models import TaskLog

BATCH_SIZE = 20
FLUSH_INTERVAL_SECONDS = 1.0
MAX_MESSAGE_LENGTH = 4000
MAX_EXCEPTION_LENGTH = 8000

# служебные поля structlog и то, что уже лежит в отдельных колонках task_logs / task_runs
_SKIPPED_KEYS = frozenset(
    {
        "event",
        "level",
        "logger",
        "timestamp",
        "service",
        "run_id",
        "task_type",
        "task_name",
        "celery_task_id",
        "exc_info",
        "stack_info",
        "_record",
        "_from_structlog",
    }
)


def _format_exc_info(value: Any) -> str | None:
    if value is True:
        value = sys.exc_info()
    if isinstance(value, BaseException):
        value = (type(value), value, value.__traceback__)
    if isinstance(value, tuple) and len(value) == 3 and value[0] is not None:
        return "".join(traceback.format_exception(*value))[-MAX_EXCEPTION_LENGTH:]
    return None


class TaskLogHandler(logging.Handler):
    def __init__(self, run_id: int, level: int = logging.INFO) -> None:
        super().__init__(level)
        self._run_id = run_id
        self._buffer: list[dict[str, Any]] = []
        self._last_flush = time.monotonic()
        self._flushing = False
        self._masker = SecretMasker(get_settings().secret_values())

    def emit(self, record: logging.LogRecord) -> None:
        # записи, порождённые самой вставкой в БД, игнорируются, чтобы не уйти в рекурсию
        if self._flushing:
            return
        try:
            self._buffer.append(self._to_row(record))
            if (
                len(self._buffer) >= BATCH_SIZE
                or time.monotonic() - self._last_flush >= FLUSH_INTERVAL_SECONDS
            ):
                self.flush()
        except Exception as exc:
            self._report(exc)

    def _to_row(self, record: logging.LogRecord) -> dict[str, Any]:
        extra: dict[str, Any]
        if isinstance(record.msg, dict):
            # запись из structlog: msg — это event dict
            event = record.msg
            message = str(event.get("event", ""))
            extra = {k: v for k, v in event.items() if k not in _SKIPPED_KEYS}
            exception = _format_exc_info(event.get("exc_info"))
        else:
            message = record.getMessage()
            extra = {}
            exception = _format_exc_info(record.exc_info)
        if exception:
            extra["exception"] = exception
        safe_extra = json.loads(json.dumps(extra, default=str, ensure_ascii=False))
        return {
            "run_id": self._run_id,
            "ts": datetime.fromtimestamp(record.created, UTC),
            "level": record.levelname.lower(),
            "logger": record.name[:255],
            "message": self._masker.scrub(message)[:MAX_MESSAGE_LENGTH],
            "extra": self._masker.scrub(safe_extra),
        }

    def flush(self) -> None:
        if self._flushing or not self._buffer:
            return
        rows, self._buffer = self._buffer, []
        self._last_flush = time.monotonic()
        self._flushing = True
        try:
            with get_sync_engine().begin() as connection:
                connection.execute(insert(TaskLog), rows)
        except Exception as exc:
            self._report(exc, lost=len(rows))
        finally:
            self._flushing = False

    def close(self) -> None:
        self.flush()
        super().close()

    def _report(self, exc: Exception, lost: int = 1) -> None:
        # в logging писать нельзя (рекурсия), поэтому напрямую в stderr
        print(
            json.dumps(
                {
                    "event": "task_log_write_failed",
                    "level": "error",
                    "run_id": self._run_id,
                    "lost_records": lost,
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
