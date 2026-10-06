"""Общая обвязка шагов пайплайна: статус запуска, контекст логов, запись в task_logs, ретраи."""

import logging
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import structlog
from celery import Task
from celery.exceptions import Ignore, SoftTimeLimitExceeded
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.db import get_sync_sessionmaker
from app.core.errors import RetryableError
from app.core.logging import get_logger
from app.models import RunStatus, TaskRun
from app.services import runs
from app.tasks.log_handler import TaskLogHandler

log = get_logger(__name__)

RETRY_BACKOFF_BASE_SECONDS = 60
MAX_TASK_RETRIES = 3

Context = dict[str, Any]


@dataclass
class StepResult:
    stats: dict[str, Any] = field(default_factory=dict)
    # что передать следующему шагу пайплайна (digest_id, paper_ids и т.п.)
    context: Context = field(default_factory=dict)
    status: RunStatus = RunStatus.SUCCESS
    # причина для статуса skipped
    note: str | None = None


StepFn = Callable[[Session, TaskRun, Context], StepResult]


@contextmanager
def capture_task_logs(run_id: int) -> Iterator[None]:
    handler = TaskLogHandler(run_id)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)
        handler.close()


def execute_step(task: Task, run_id: int, context: Context | None, step: StepFn) -> Context:
    """Выполняет шаг и возвращает контекст для следующего шага цепочки."""
    context = dict(context or {})
    session_factory = get_sync_sessionmaker()

    with session_factory() as session:
        run = session.get(TaskRun, run_id)
        if run is None:
            log.error("run_not_found", run_id=run_id)
            raise Ignore()
        if run.status in runs.TERMINAL_STATUSES:
            # запуск отменили или пропустили, пока задача ждала в очереди
            log.info("run_already_finished", run_id=run_id, status=run.status.value)
            raise Ignore()
        runs.mark_running(session, run, task.request.id)
        task_type = run.task_type.value

    structlog.contextvars.bind_contextvars(run_id=run_id, task_type=task_type)
    started = time.perf_counter()

    with capture_task_logs(run_id):
        log.info("step_started", params=context, attempt=task.request.retries + 1)
        try:
            with session_factory() as session:
                run = session.get_one(TaskRun, run_id)
                result = step(session, run, context)
        except (RetryableError, OperationalError) as exc:
            if task.request.retries < MAX_TASK_RETRIES:
                countdown = RETRY_BACKOFF_BASE_SECONDS * 2**task.request.retries
                log.warning("step_retry_scheduled", error=str(exc), countdown_seconds=countdown)
                with session_factory() as session:
                    runs.mark_retrying(
                        session,
                        session.get_one(TaskRun, run_id),
                        str(exc),
                        task.request.retries + 1,
                    )
                raise task.retry(exc=exc, countdown=countdown) from exc
            _fail(run_id, exc, started)
            raise
        except SoftTimeLimitExceeded as exc:
            _fail(run_id, RuntimeError("Превышен лимит времени выполнения задачи"), started)
            raise exc
        except Exception as exc:
            _fail(run_id, exc, started)
            raise

        with session_factory() as session:
            runs.finish_run(
                session,
                session.get_one(TaskRun, run_id),
                result.status,
                stats=result.stats,
                error=result.note,
            )
        log.info(
            "step_finished",
            status=result.status.value,
            stats=result.stats,
            note=result.note,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )

    return {**context, **result.context}


def _fail(run_id: int, exc: BaseException, started: float) -> None:
    message = str(exc) or type(exc).__name__
    log.error(
        "step_failed",
        error=message,
        error_type=type(exc).__name__,
        duration_ms=round((time.perf_counter() - started) * 1000),
        exc_info=exc,
    )
    with get_sync_sessionmaker()() as session:
        runs.finish_run(
            session,
            session.get_one(TaskRun, run_id),
            RunStatus.FAILED,
            error=message,
            traceback="".join(traceback.format_exception(exc)),
        )
