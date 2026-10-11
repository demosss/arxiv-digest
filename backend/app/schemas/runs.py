from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.models.enums import DigestContent, RunStatus, RunTrigger, TaskType
from app.schemas.common import Timestamps
from app.services.periods import InvalidPeriodError, resolve_period, run_params

# задачи, в которых собирается PDF
DIGEST_CONTENT_TASKS = frozenset({TaskType.FULL_PIPELINE, TaskType.BUILD_DIGEST})


class RunCreate(BaseModel):
    """Ручной запуск задачи. Без периода берутся прошедшие сутки (UTC)."""

    task_type: TaskType
    period_start: datetime | None = Field(default=None, description="Начало периода (с таймзоной)")
    period_end: datetime | None = Field(default=None, description="Конец периода, включительно")
    digest_id: int | None = Field(default=None, description="Для send_digest: какой дайджест")
    paper_ids: list[int] | None = Field(default=None, description="Для summarize: какие статьи")
    force: bool = Field(default=False, description="Для send_digest: отправить повторно")
    retry_failed: bool = Field(
        default=False, description="Для summarize: взять и статьи, упавшие на суммаризации"
    )
    fill_abstracts: bool = Field(
        default=False,
        description="Для summarize: только дописать аннотации статьям, у которых резюме уже есть",
    )
    digest_content: DigestContent | None = Field(
        default=None,
        description="Для full_pipeline и build_digest: состав карточки (иначе DIGEST_CONTENT)",
    )

    @model_validator(mode="after")
    def check_period(self) -> "RunCreate":
        if self.task_type == TaskType.TRANSLATE:
            raise ValueError("Перевод запускается из карточки статьи: POST /papers/{id}/translate")
        try:
            if self.period_start is not None or self.period_end is not None:
                resolve_period(self.period_start, self.period_end)
        except InvalidPeriodError as exc:
            raise ValueError(str(exc)) from exc
        return self

    def to_params(self) -> dict[str, Any]:
        """Параметры запуска. Период фиксируется в момент запуска, а не выполнения."""
        params = run_params(self.task_type, self.period_start, self.period_end)
        if self.digest_id is not None:
            params["digest_id"] = self.digest_id
        if self.paper_ids:
            params["paper_ids"] = self.paper_ids
        if self.force:
            params["force"] = True
        if self.retry_failed:
            params["retry_failed"] = True
        # в цепочке full_pipeline этот флаг подменил бы суммаризацию новых статей
        if self.fill_abstracts and self.task_type == TaskType.SUMMARIZE:
            params["fill_abstracts"] = True
        if self.digest_content is not None and self.task_type in DIGEST_CONTENT_TASKS:
            params["digest_content"] = self.digest_content.value
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
