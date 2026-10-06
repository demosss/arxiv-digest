import asyncio
import time
from datetime import timedelta
from typing import Any

from fastapi import APIRouter
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import health
from app.api.deps import SessionDep
from app.core.config import get_settings
from app.core.db import utcnow
from app.models import (
    SMTP_SETTINGS_ID,
    Digest,
    DigestStatus,
    Paper,
    RunStatus,
    Schedule,
    SmtpSettings,
    Summary,
    TaskRun,
    TaskType,
)
from app.schemas.dashboard import DashboardCounters, DashboardOut, SmtpStatus
from app.schemas.papers import LLMHealthOut
from app.schemas.runs import RunOut
from app.schemas.schedules import ScheduleOut
from app.services.runs import ACTIVE_STATUSES

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

COUNTERS_WINDOW = timedelta(days=7)
UPCOMING_SCHEDULES = 5
RECENT_RUNS = 10
# дашборд обновляется часто, а проверка недоступной модели может занять до 10 секунд
LLM_HEALTH_TTL_SECONDS = 30.0
_llm_cache: tuple[float, LLMHealthOut] | None = None


async def cached_llm_health() -> LLMHealthOut:
    global _llm_cache
    if _llm_cache is not None and time.monotonic() - _llm_cache[0] < LLM_HEALTH_TTL_SECONDS:
        return _llm_cache[1]
    result = await asyncio.to_thread(health.check_llm)
    _llm_cache = (time.monotonic(), result)
    return result


def reset_llm_cache() -> None:
    global _llm_cache
    _llm_cache = None


async def _smtp_status(session: AsyncSession) -> SmtpStatus:
    row = await session.get(SmtpSettings, SMTP_SETTINGS_ID)
    password_set = get_settings().smtp_password_set
    is_configured = bool(row and row.host and row.from_email)
    error = None
    if not is_configured:
        error = "Не заданы сервер и адрес отправителя"
    elif row is not None and row.username and not password_set:
        error = "Задан логин, но не задан SMTP_PASSWORD"
    return SmtpStatus(
        ok=error is None,
        is_configured=is_configured,
        password_set=password_set,
        host=row.host if row else "",
        error=error,
    )


async def _count(session: AsyncSession, stmt: Select[Any]) -> int:
    return int(await session.scalar(stmt) or 0)


@router.get("", response_model=DashboardOut)
async def dashboard(session: SessionDep) -> DashboardOut:
    since = utcnow() - COUNTERS_WINDOW
    top_level = TaskRun.parent_run_id.is_(None)

    counters = DashboardCounters(
        papers_collected=await _count(
            session, select(func.count()).select_from(Paper).where(Paper.created_at >= since)
        ),
        papers_summarized=await _count(
            session, select(func.count()).select_from(Summary).where(Summary.updated_at >= since)
        ),
        digests_sent=await _count(
            session,
            select(func.count())
            .select_from(Digest)
            .where(
                Digest.status.in_([DigestStatus.SENT, DigestStatus.PARTIALLY_SENT]),
                Digest.updated_at >= since,
            ),
        ),
        runs_success=await _count(
            session,
            select(func.count())
            .select_from(TaskRun)
            .where(top_level, TaskRun.status == RunStatus.SUCCESS, TaskRun.created_at >= since),
        ),
        runs_failed=await _count(
            session,
            select(func.count())
            .select_from(TaskRun)
            .where(top_level, TaskRun.status == RunStatus.FAILED, TaskRun.created_at >= since),
        ),
    )
    pipeline_running = (
        await session.scalar(
            select(TaskRun.id)
            .where(
                TaskRun.task_type == TaskType.FULL_PIPELINE,
                TaskRun.status.in_(ACTIVE_STATUSES),
            )
            .limit(1)
        )
    ) is not None
    schedules = await session.scalars(
        select(Schedule)
        .where(Schedule.is_active.is_(True), Schedule.next_fire_at.is_not(None))
        .order_by(Schedule.next_fire_at)
        .limit(UPCOMING_SCHEDULES)
    )
    recent = await session.scalars(
        select(TaskRun).where(top_level).order_by(TaskRun.id.desc()).limit(RECENT_RUNS)
    )
    return DashboardOut(
        counters=counters,
        llm=await cached_llm_health(),
        smtp=await _smtp_status(session),
        pipeline_running=pipeline_running,
        upcoming_schedules=[ScheduleOut.model_validate(s) for s in schedules],
        recent_runs=[RunOut.model_validate(r) for r in recent],
        timezone=get_settings().tz,
    )
