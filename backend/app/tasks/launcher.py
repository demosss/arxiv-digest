"""Постановка запусков в очередь: общая точка входа для API (ручной запуск) и планировщика."""

import uuid
from typing import Any

from celery import Task
from sqlalchemy.orm import Session

from app.core.errors import BadRequestError
from app.core.logging import get_logger
from app.models import RunStatus, RunTrigger, TaskRun, TaskType
from app.services import runs
from app.tasks.celery_app import celery_app
from app.tasks.pipeline import (
    build_digest_task,
    collect_task,
    send_digest_task,
    summarize_task,
)

log = get_logger(__name__)

STEP_TASKS: dict[TaskType, Task] = {
    TaskType.COLLECT: collect_task,
    TaskType.SUMMARIZE: summarize_task,
    TaskType.BUILD_DIGEST: build_digest_task,
    TaskType.SEND_DIGEST: send_digest_task,
}


def _enqueue(session: Session, run: TaskRun, task: Task, params: dict[str, Any]) -> None:
    """Публикует задачу после коммита запуска; сбой брокера помечает запуск как failed."""
    try:
        task.apply_async(args=[params], kwargs={"run_id": run.id}, task_id=run.celery_task_id)
    except Exception as exc:
        log.error("run_enqueue_failed", run_id=run.id, error=str(exc))
        runs.finish_run(
            session,
            run,
            RunStatus.FAILED,
            error=f"Не удалось поставить задачу в очередь: {type(exc).__name__}",
        )
        return
    log.info(
        "run_enqueued",
        run_id=run.id,
        task_type=run.task_type.value,
        trigger=run.trigger.value,
        schedule_id=run.schedule_id,
    )


def launch_run(
    session: Session,
    *,
    task_type: TaskType,
    trigger: RunTrigger,
    params: dict[str, Any] | None = None,
    schedule_id: int | None = None,
) -> TaskRun:
    """Создаёт запись запуска и ставит задачу в очередь."""
    params = params or {}
    task = STEP_TASKS.get(task_type)
    if task is None:
        raise BadRequestError(
            f"Тип задачи «{task_type.value}» не поддерживается", code="unsupported_task_type"
        )
    run = runs.create_run(
        session,
        task_type=task_type,
        trigger=trigger,
        params=params,
        schedule_id=schedule_id,
        celery_task_id=str(uuid.uuid4()),
    )
    session.commit()
    _enqueue(session, run, task, params)
    return run


def cancel_run(session: Session, run: TaskRun) -> list[int]:
    """Отменяет запуск и его шаги: отзывает задачи Celery и ставит статус cancelled."""
    targets = [run, *run.children]
    cancelled: list[int] = []
    for target in targets:
        if target.status not in runs.ACTIVE_STATUSES:
            continue
        if target.celery_task_id:
            celery_app.control.revoke(target.celery_task_id, terminate=True, signal="SIGTERM")
        runs.finish_run(session, target, RunStatus.CANCELLED, error="Отменено пользователем")
        cancelled.append(target.id)
    session.refresh(run)
    log.info("run_cancelled", run_id=run.id, cancelled_run_ids=cancelled)
    return cancelled
