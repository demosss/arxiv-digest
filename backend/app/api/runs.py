from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import ColumnElement, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.api.deps import CurrentUser, PaginationDep, SessionDep, paginate
from app.api.logs import LogLevel, LogLimit, fetch_logs
from app.core.db import get_sync_sessionmaker
from app.core.errors import ConflictError, NotFoundError
from app.models import RunStatus, RunTrigger, TaskLog, TaskRun, TaskType
from app.schemas.common import DeletedCount, Page
from app.schemas.runs import LogPage, RunCancelResponse, RunCreate, RunDetail, RunOut
from app.services import runs as runs_service
from app.services.audit import audit
from app.tasks import launcher

router = APIRouter(prefix="/runs", tags=["runs"])


async def load_run(session: AsyncSession, run_id: int) -> TaskRun:
    run = await session.scalar(
        select(TaskRun)
        .where(TaskRun.id == run_id)
        .options(selectinload(TaskRun.children))
        .execution_options(populate_existing=True)
    )
    if run is None:
        raise NotFoundError("Запуск не найден")
    return run


async def ensure_no_active_runs(session: AsyncSession) -> None:
    """Статьи и дайджесты нельзя удалять, пока с ними может работать задача."""
    active = await session.scalar(
        select(TaskRun.id).where(TaskRun.status.in_(runs_service.ACTIVE_STATUSES)).limit(1)
    )
    if active is not None:
        raise ConflictError(
            "Есть активные запуски: дождитесь их завершения или отмените", code="runs_active"
        )


def _conditions(
    task_type: TaskType | None,
    run_status: RunStatus | None,
    trigger: RunTrigger | None,
    schedule_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if task_type is not None:
        conditions.append(TaskRun.task_type == task_type)
    if run_status is not None:
        conditions.append(TaskRun.status == run_status)
    if trigger is not None:
        conditions.append(TaskRun.trigger == trigger)
    if schedule_id is not None:
        conditions.append(TaskRun.schedule_id == schedule_id)
    if date_from is not None:
        conditions.append(TaskRun.created_at >= date_from)
    if date_to is not None:
        conditions.append(TaskRun.created_at <= date_to)
    return conditions


def launch_manual_run(task_type: TaskType, params: dict[str, Any]) -> int:
    with get_sync_sessionmaker()() as session:
        run = launcher.launch_run(
            session, task_type=task_type, trigger=RunTrigger.MANUAL, params=params
        )
        return run.id


def _cancel(run_id: int) -> list[int]:
    with get_sync_sessionmaker()() as session:
        run = session.get(TaskRun, run_id)
        if run is None:
            raise NotFoundError("Запуск не найден")
        if run.status not in runs_service.ACTIVE_STATUSES:
            raise ConflictError(
                f"Запуск уже завершён со статусом «{run.status.value}»", code="run_not_active"
            )
        return launcher.cancel_run(session, run)


@router.get("", response_model=Page[RunOut])
async def list_runs(
    session: SessionDep,
    pagination: PaginationDep,
    task_type: TaskType | None = None,
    run_status: Annotated[RunStatus | None, Query(alias="status")] = None,
    trigger: RunTrigger | None = None,
    schedule_id: int | None = None,
    date_from: Annotated[datetime | None, Query(description="Создан не раньше")] = None,
    date_to: Annotated[datetime | None, Query(description="Создан не позже")] = None,
    include_steps: Annotated[
        bool, Query(description="Показывать шаги пайплайнов отдельными строками")
    ] = False,
) -> Page[RunOut]:
    stmt = (
        select(TaskRun)
        .where(*_conditions(task_type, run_status, trigger, schedule_id, date_from, date_to))
        .order_by(TaskRun.id.desc())
    )
    if not include_steps:
        stmt = stmt.where(TaskRun.parent_run_id.is_(None))
    items, total = await paginate(session, stmt, pagination)
    return Page(
        items=[RunOut.model_validate(r) for r in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.post("", response_model=RunDetail, status_code=status.HTTP_202_ACCEPTED)
async def create_run(payload: RunCreate, session: SessionDep, user: CurrentUser) -> TaskRun:
    """Ручной запуск шага или всего пайплайна. Задача выполняется в фоне."""
    params = payload.to_params()
    run_id = await run_in_threadpool(launch_manual_run, payload.task_type, params)
    audit("launch", "run", run_id, user.username, task_type=payload.task_type.value, params=params)
    return await load_run(session, run_id)


@router.delete("", response_model=DeletedCount)
async def clear_runs(
    session: SessionDep,
    user: CurrentUser,
    task_type: TaskType | None = None,
    run_status: Annotated[RunStatus | None, Query(alias="status")] = None,
    trigger: RunTrigger | None = None,
    schedule_id: int | None = None,
    date_from: Annotated[datetime | None, Query(description="Создан не раньше")] = None,
    date_to: Annotated[datetime | None, Query(description="Создан не позже")] = None,
) -> DeletedCount:
    """Очищает историю: удаляет завершённые запуски под фильтрами вместе с шагами и логами.

    Активные запуски (и пайплайны с активными шагами) не затрагиваются.
    """
    step = aliased(TaskRun)
    has_active_step = (
        select(step.id)
        .where(step.parent_run_id == TaskRun.id, step.status.in_(runs_service.ACTIVE_STATUSES))
        .correlate(TaskRun)
        .exists()
    )
    result = await session.execute(
        delete(TaskRun)
        .where(
            TaskRun.parent_run_id.is_(None),
            TaskRun.status.not_in(runs_service.ACTIVE_STATUSES),
            ~has_active_step,
            *_conditions(task_type, run_status, trigger, schedule_id, date_from, date_to),
        )
        .returning(TaskRun.id)
    )
    deleted = len(result.scalars().all())
    await session.commit()
    audit("clear", "run", None, user.username, deleted=deleted)
    return DeletedCount(deleted=deleted)


@router.get("/{run_id}", response_model=RunDetail)
async def get_run(run_id: int, session: SessionDep) -> TaskRun:
    return await load_run(session, run_id)


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(run_id: int, session: SessionDep, user: CurrentUser) -> None:
    """Удаляет завершённый запуск вместе с шагами и логами."""
    run = await load_run(session, run_id)
    family = [run, *run.children]
    if run.parent_run_id is not None:
        parent = await session.get(TaskRun, run.parent_run_id)
        if parent is not None:
            family.append(parent)
    if any(member.status in runs_service.ACTIVE_STATUSES for member in family):
        raise ConflictError("Запуск ещё выполняется: сначала отмените его", code="run_active")
    task_type = run.task_type.value
    await session.execute(delete(TaskRun).where(TaskRun.id == run_id))
    await session.commit()
    audit("delete", "run", run_id, user.username, task_type=task_type)


@router.post("/{run_id}/cancel", response_model=RunCancelResponse)
async def cancel_run(run_id: int, session: SessionDep, user: CurrentUser) -> RunCancelResponse:
    cancelled = await run_in_threadpool(_cancel, run_id)
    audit("cancel", "run", run_id, user.username, cancelled_run_ids=cancelled)
    run = await load_run(session, run_id)
    return RunCancelResponse(run=RunDetail.model_validate(run), cancelled_run_ids=cancelled)


@router.get("/{run_id}/logs", response_model=LogPage)
async def get_run_logs(
    run_id: int,
    session: SessionDep,
    level: LogLevel = None,
    after_id: Annotated[
        int | None, Query(description="Только записи с id больше указанного (для автообновления)")
    ] = None,
    include_steps: Annotated[bool, Query(description="Вместе с логами шагов пайплайна")] = True,
    limit: LogLimit = 500,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LogPage:
    run = await load_run(session, run_id)
    run_ids = [run.id]
    if include_steps:
        run_ids += [child.id for child in run.children]
    stmt = select(TaskLog).where(TaskLog.run_id.in_(run_ids))
    if after_id is not None:
        stmt = stmt.where(TaskLog.id > after_id)
    return await fetch_logs(
        session, stmt, level=level, limit=limit, offset=offset, newest_first=False
    )
