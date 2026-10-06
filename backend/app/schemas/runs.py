from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.models.enums import RunStatus, RunTrigger, TaskType
from app.schemas.common import Timestamps
from app.services.periods import InvalidPeriodError, period_to_context, resolve_period

# шаги, для которых имеет смысл период выборки
_PERIOD_TASKS = frozenset({TaskType.FULL_PIPELINE, TaskType.COLLECT, TaskType.BUILD_DIGEST})


class RunCreate(BaseModel):
    """Ручной запуск задачи. Без периода берутся прошедшие сутки (UTC)."""

    task_type: TaskType
    period_start: datetime | None = Field(default=None, description="Начало периода (с таймзоной)")
    period_end: datetime | None = Field(default=None, description="Конец периода, включительно")
    digest_id: int | None = Field(default=None, description="Для send_digest: какой дайджест")
    paper_ids: list[int] | None = Field(default=None, description="Для summarize: какие статьи")
    force: bool = Field(default=False, description="Для send_digest: отправить повторно")

    @model_validator(mode="after")
    def check_period(self) -> "RunCreate":
        try:
            if self.period_start is not None or self.period_end is not None:
                resolve_period(self.period_start, self.period_end)
        except InvalidPeriodError as exc:
            raise ValueError(str(exc)) from exc
        return self

    def to_params(self) -> dict[str, Any]:
        """Параметры запуска. Период фиксируется в момент запуска, а не выполнения."""
        params: dict[str, Any] = {}
        if self.task_type in _PERIOD_TASKS:
            params.update(period_to_context(*resolve_period(self.period_start, self.period_end)))
        if self.digest_id is not None:
            params["digest_id"] = self.digest_id
        if self.paper_ids:
            params["paper_ids"] = self.paper_ids
        if self.force:
            params["force"] = True
        return params


class RunOut(Timestamps):
    id: int
    task_type: TaskType
    trigger: RunTrigger
    schedule_id: int | None
    parent_run_id: int | None
    status: RunStatus
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    params: dict[str, Any]
    stats: dict[str, Any]
    error: str | None


class RunDetail(RunOut):
    celery_task_id: str | None
    traceback: str | None
    children: list[RunOut]


class RunCancelResponse(BaseModel):
    run: RunDetail
    cancelled_run_ids: list[int]


class LogOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    run_id: int
    ts: datetime
    level: str
    logger: str
    message: str
    extra: dict[str, Any]


class LogPage(BaseModel):
    items: list[LogOut]
    total: int
    # id последней записи страницы: для догрузки новых записей через after_id
    last_id: int | None
