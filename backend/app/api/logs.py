from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import SessionDep
from app.models import TaskLog, TaskRun, TaskType
from app.schemas.runs import LogOut, LogPage

router = APIRouter(prefix="/logs", tags=["logs"])

LEVELS = ("debug", "info", "warning", "error", "critical")
LogLevel = Annotated[
    Literal["debug", "info", "warning", "error", "critical"] | None,
    Query(description="Минимальный уровень: warning вернёт warning, error и critical"),
]
LogLimit = Annotated[int, Query(ge=1, le=2000)]


async def fetch_logs(
    session: AsyncSession,
    stmt: Select[Any],
    *,
    level: str | None,
    limit: int,
    offset: int,
    newest_first: bool,
) -> LogPage:
    if level is not None:
        stmt = stmt.where(TaskLog.level.in_(LEVELS[LEVELS.index(level) :]))
    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    order = (TaskLog.ts.desc(), TaskLog.id.desc()) if newest_first else (TaskLog.ts, TaskLog.id)
    rows = (await session.scalars(stmt.order_by(*order).limit(limit).offset(offset))).all()
    return LogPage(
        items=[LogOut.model_validate(row) for row in rows],
        total=total or 0,
        last_id=max((row.id for row in rows), default=None),
    )


@router.get("", response_model=LogPage)
async def search_logs(
    session: SessionDep,
    q: Annotated[str | None, Query(description="Поиск по тексту сообщения")] = None,
    level: LogLevel = None,
    run_id: int | None = None,
    task_type: TaskType | None = None,
    logger: Annotated[str | None, Query(description="Префикс имени логгера")] = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: LogLimit = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LogPage:
    """Общий поиск по логам всех запусков, новые записи сверху."""
    stmt = select(TaskLog)
    if q:
        stmt = stmt.where(TaskLog.message.icontains(q, autoescape=True))
    if run_id is not None:
        stmt = stmt.where(TaskLog.run_id == run_id)
    if task_type is not None:
        stmt = stmt.join(TaskRun, TaskRun.id == TaskLog.run_id).where(
            TaskRun.task_type == task_type
        )
    if logger:
        stmt = stmt.where(TaskLog.logger.startswith(logger, autoescape=True))
    if date_from is not None:
        stmt = stmt.where(TaskLog.ts >= date_from)
    if date_to is not None:
        stmt = stmt.where(TaskLog.ts <= date_to)
    return await fetch_logs(
        session, stmt, level=level, limit=limit, offset=offset, newest_first=True
    )
