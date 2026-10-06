from pydantic import BaseModel

from app.schemas.papers import LLMHealthOut
from app.schemas.runs import RunOut
from app.schemas.schedules import ScheduleOut


class SmtpStatus(BaseModel):
    ok: bool
    is_configured: bool
    password_set: bool
    host: str
    error: str | None


class DashboardCounters(BaseModel):
    """Счётчики за последние 7 дней."""

    papers_collected: int
    papers_summarized: int
    digests_sent: int
    runs_success: int
    runs_failed: int


class DashboardOut(BaseModel):
    counters: DashboardCounters
    llm: LLMHealthOut
    smtp: SmtpStatus
    pipeline_running: bool
    upcoming_schedules: list[ScheduleOut]
    recent_runs: list[RunOut]
    timezone: str
