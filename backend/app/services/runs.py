"""Жизненный цикл запусков (task_runs). Sync-сессия: вызывается из Celery и планировщика."""

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.core.logging import get_logger
from app.models import RunStatus, RunTrigger, TaskLog, TaskRun, TaskType

log = get_logger(__name__)

ACTIVE_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING)
TERMINAL_STATUSES = (
    RunStatus.SUCCESS,
    RunStatus.FAILED,
    RunStatus.SKIPPED,
    RunStatus.CANCELLED,
)
MAX_ERROR_LENGTH = 4000


def create_run(
    session: Session,
    *,
    profile_id: int,
    task_type: TaskType,
    trigger: RunTrigger,
    params: dict[str, Any] | None = None,
    schedule_id: int | None = None,
    parent_run_id: int | None = None,
    status: RunStatus = RunStatus.QUEUED,
    celery_task_id: str | None = None,
) -> TaskRun:
    run = TaskRun(
        profile_id=profile_id,
        task_type=task_type,
        trigger=trigger,
        params=params or {},
        stats={},
        schedule_id=schedule_id,
        parent_run_id=parent_run_id,
        status=status,
        celery_task_id=celery_task_id,
    )
    session.add(run)
    session.flush()
    return run


def _set_finished(run: TaskRun) -> None:
    run.finished_at = utcnow()
    if run.started_at is not None:
        run.duration_ms = int((run.finished_at - run.started_at).total_seconds() * 1000)


def mark_running(session: Session, run: TaskRun, celery_task_id: str | None) -> None:
    now = utcnow()
    run.status = RunStatus.RUNNING
    run.started_at = run.started_at or now
    run.celery_task_id = celery_task_id or run.celery_task_id
    run.error = None
    if run.parent_run_id is not None:
        parent = session.get(TaskRun, run.parent_run_id, with_for_update=True)
        if parent is not None and parent.status == RunStatus.QUEUED:
            parent.status = RunStatus.RUNNING
            parent.started_at = now
    session.commit()


def mark_retrying(session: Session, run: TaskRun, error: str, attempt: int) -> None:
    """Задача будет повторена Celery: запуск возвращается в очередь, причина остаётся в error."""
    run.status = RunStatus.QUEUED
    run.error = f"Повтор {attempt}: {error}"[:MAX_ERROR_LENGTH]
    session.commit()


def finish_run(
    session: Session,
    run: TaskRun,
    status: RunStatus,
    *,
    stats: dict[str, Any] | None = None,
    error: str | None = None,
    traceback: str | None = None,
) -> None:
    """Переводит запуск в конечный статус и обновляет родительский пайплайн."""
    run.status = status
    if stats is not None:
        run.stats = stats
    run.error = error[:MAX_ERROR_LENGTH] if error else None
    run.traceback = traceback
    _set_finished(run)
    session.flush()
    if run.parent_run_id is not None:
        _sync_parent(session, run)
    session.commit()


def _sync_parent(session: Session, child: TaskRun) -> None:
    """Статус пайплайна выводится из шагов: ошибка шага останавливает остальные."""
    parent = session.get(TaskRun, child.parent_run_id, with_for_update=True)
    if parent is None or parent.status in TERMINAL_STATUSES:
        return
    siblings = session.scalars(
        select(TaskRun).where(TaskRun.parent_run_id == parent.id).order_by(TaskRun.id)
    ).all()

    if child.status in (RunStatus.FAILED, RunStatus.CANCELLED):
        reason = "ошибкой" if child.status == RunStatus.FAILED else "отменой"
        for sibling in siblings:
            if sibling.status == RunStatus.QUEUED:
                sibling.status = RunStatus.SKIPPED
                sibling.error = f"Пропущено: шаг «{child.task_type.value}» завершился {reason}"
                _set_finished(sibling)

    if any(s.status in ACTIVE_STATUSES for s in siblings):
        return

    statuses = {s.status for s in siblings}
    if RunStatus.FAILED in statuses:
        parent.status = RunStatus.FAILED
        failed = next(s for s in siblings if s.status == RunStatus.FAILED)
        parent.error = f"Шаг «{failed.task_type.value}»: {failed.error}"[:MAX_ERROR_LENGTH]
    elif RunStatus.CANCELLED in statuses:
        parent.status = RunStatus.CANCELLED
    else:
        parent.status = RunStatus.SUCCESS
    parent.stats = {s.task_type.value: s.stats for s in siblings}
    _set_finished(parent)
    log.info(
        "pipeline_finished",
        pipeline_run_id=parent.id,
        status=parent.status.value,
        steps={s.task_type.value: s.status.value for s in siblings},
    )


def fail_stale_runs(session: Session, stale_hours: int) -> list[int]:
    """Помечает failed запуски, у которых давно нет признаков жизни.

    Признак жизни — изменение самого запуска, его шагов или новая запись в task_logs.
    Так «убитый» воркер (OOM, kill -9) не блокирует следующие пайплайны навсегда.
    """
    now = utcnow()
    threshold = now - timedelta(hours=stale_hours)
    candidates = session.scalars(
        select(TaskRun).where(
            TaskRun.parent_run_id.is_(None),
            TaskRun.status.in_(ACTIVE_STATUSES),
            TaskRun.updated_at < threshold,
        )
    ).all()

    failed: list[int] = []
    for run in candidates:
        family = [run, *run.children]
        last_log = session.scalar(
            select(func.max(TaskLog.ts)).where(TaskLog.run_id.in_([r.id for r in family]))
        )
        last_activity = max([r.updated_at for r in family] + ([last_log] if last_log else []))
        if last_activity >= threshold:
            continue
        reason = f"Запуск завис: нет активности более {stale_hours} ч"
        for member in reversed(family):
            if member.status in ACTIVE_STATUSES:
                member.status = RunStatus.FAILED
                member.error = reason
                _set_finished(member)
        failed.append(run.id)
        log.error("run_marked_stale", run_id=run.id, last_activity=last_activity.isoformat())
    if failed:
        session.flush()
    return failed
