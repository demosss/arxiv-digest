"""Постановка запусков в очередь: общая точка входа для API (ручной запуск) и планировщика."""

import uuid
from typing import Any

from celery import Task, chain
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import BadRequestError
from app.core.logging import get_logger
from app.models import RunStatus, RunTrigger, TaskRun, TaskType
from app.services import runs
from app.services.translation.service import fail_orphaned
from app.tasks.celery_app import celery_app
from app.tasks.pipeline import (
    build_digest_task,
    collect_task,
    send_digest_task,
    send_paper_task,
    summarize_task,
    translate_task,
)

log = get_logger(__name__)

STEP_TASKS: dict[TaskType, Task] = {
    TaskType.COLLECT: collect_task,
    TaskType.SUMMARIZE: summarize_task,
    TaskType.BUILD_DIGEST: build_digest_task,
    TaskType.SEND_DIGEST: send_digest_task,
    TaskType.TRANSLATE: translate_task,
    TaskType.SEND_PAPER: send_paper_task,
}
PIPELINE_STEPS = (
    TaskType.COLLECT,
    TaskType.SUMMARIZE,
    TaskType.BUILD_DIGEST,
    TaskType.SEND_DIGEST,
)
# ключ advisory lock PostgreSQL: сериализует проверку «пайплайн уже идёт» и создание запуска
PIPELINE_LOCK_KEY = 0x6172786976  # "arxiv"


def _enqueue_failed(session: Session, run: TaskRun, exc: Exception) -> None:
    log.error("run_enqueue_failed", run_id=run.id, error=str(exc))
    # состояние перечитывается из БД: трогаем только те запуски, которые всё ещё не завершены
    session.expire_all()
    for target in (*run.children, run):
        if target.status in runs.ACTIVE_STATUSES:
            runs.finish_run(
                session,
                target,
                RunStatus.FAILED,
                error=f"Не удалось поставить задачу в очередь: {type(exc).__name__}",
            )
    session.refresh(run)


def _log_enqueued(run: TaskRun) -> None:
    log.info(
        "run_enqueued",
        run_id=run.id,
        profile_id=run.profile_id,
        task_type=run.task_type.value,
        trigger=run.trigger.value,
        schedule_id=run.schedule_id,
    )


def publish_pipeline(steps: list[TaskRun], params: dict[str, Any]) -> None:
    """Публикует цепочку шагов: результат каждого шага — контекст для следующего."""
    signatures = []
    for index, step in enumerate(steps):
        task = STEP_TASKS[step.task_type]
        args = (params,) if index == 0 else ()
        signatures.append(task.s(*args, run_id=step.id).set(task_id=step.celery_task_id))
    chain(*signatures).apply_async()


def _launch_pipeline(
    session: Session,
    profile_id: int,
    trigger: RunTrigger,
    params: dict[str, Any],
    schedule_id: int | None,
) -> TaskRun:
    """Запускает full_pipeline профиля, если он ещё не выполняется; иначе — запуск skipped.

    Пайплайны разных профилей друг другу не мешают: их шаги встают в общие очереди.
    """
    # блокировка до конца транзакции: два одновременных запуска не проскочат проверку оба
    session.execute(select(func.pg_advisory_xact_lock(PIPELINE_LOCK_KEY)))
    runs.fail_stale_runs(session, get_settings().pipeline_stale_hours)

    active_id = session.scalar(
        select(TaskRun.id)
        .where(
            TaskRun.profile_id == profile_id,
            TaskRun.task_type == TaskType.FULL_PIPELINE,
            TaskRun.status.in_(runs.ACTIVE_STATUSES),
        )
        .limit(1)
    )
    if active_id is not None:
        reason = f"Пайплайн уже выполняется (запуск #{active_id}), параллельный запуск пропущен"
        run = runs.create_run(
            session,
            profile_id=profile_id,
            task_type=TaskType.FULL_PIPELINE,
            trigger=trigger,
            params=params,
            schedule_id=schedule_id,
        )
        runs.finish_run(session, run, RunStatus.SKIPPED, error=reason)
        log.warning(
            "pipeline_skipped_already_running",
            run_id=run.id,
            active_run_id=active_id,
            profile_id=profile_id,
            trigger=trigger.value,
            schedule_id=schedule_id,
        )
        return run

    parent = runs.create_run(
        session,
        profile_id=profile_id,
        task_type=TaskType.FULL_PIPELINE,
        trigger=trigger,
        params=params,
        schedule_id=schedule_id,
    )
    steps = [
        runs.create_run(
            session,
            profile_id=profile_id,
            task_type=step,
            trigger=trigger,
            schedule_id=schedule_id,
            parent_run_id=parent.id,
            celery_task_id=str(uuid.uuid4()),
        )
        for step in PIPELINE_STEPS
    ]
    session.commit()

    try:
        publish_pipeline(steps, params)
    except Exception as exc:
        _enqueue_failed(session, parent, exc)
        return parent
    _log_enqueued(parent)
    return parent


def launch_run(
    session: Session,
    *,
    profile_id: int,
    task_type: TaskType,
    trigger: RunTrigger,
    params: dict[str, Any] | None = None,
    schedule_id: int | None = None,
) -> TaskRun:
    """Создаёт запись запуска в профиле и ставит задачу в очередь. Коммитит сессию."""
    params = params or {}
    if task_type == TaskType.FULL_PIPELINE:
        return _launch_pipeline(session, profile_id, trigger, params, schedule_id)

    task = STEP_TASKS.get(task_type)
    if task is None:
        raise BadRequestError(
            f"Тип задачи «{task_type.value}» не поддерживается", code="unsupported_task_type"
        )
    run = runs.create_run(
        session,
        profile_id=profile_id,
        task_type=task_type,
        trigger=trigger,
        params=params,
        schedule_id=schedule_id,
        celery_task_id=str(uuid.uuid4()),
    )
    session.commit()
    try:
        task.apply_async(args=[params], kwargs={"run_id": run.id}, task_id=run.celery_task_id)
    except Exception as exc:
        _enqueue_failed(session, run, exc)
        return run
    _log_enqueued(run)
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
    # отозванная задача перевода завершается сигналом и свой статус уже не обновит
    fail_orphaned(session)
    session.commit()
    session.refresh(run)
    log.info("run_cancelled", run_id=run.id, cancelled_run_ids=cancelled)
    return cancelled
