from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.enums import PAPER_TASKS, TaskType
from app.schemas.common import Timestamps
from app.services.scheduler import DEFAULT_TIMEZONE, get_timezone, validate_cron


def _validate_timezone(value: str) -> str:
    get_timezone(value)
    return value


def _validate_task_type(value: TaskType | None) -> TaskType | None:
    if value in PAPER_TASKS:
        raise ValueError("Перевод и отправку статьи нельзя запускать по расписанию")
    return value


def _validate_name(value: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise ValueError("Название не может быть пустым")
    return value


class ScheduleCreate(BaseModel):
    name: str = Field(max_length=255)
    task_type: TaskType
    cron_expression: str = Field(max_length=120)
    timezone: str = Field(default=DEFAULT_TIMEZONE, max_length=64)
    is_active: bool = True

    check_name = field_validator("name")(_validate_name)
    check_task_type = field_validator("task_type")(_validate_task_type)
    check_cron = field_validator("cron_expression")(validate_cron)
    check_timezone = field_validator("timezone")(_validate_timezone)


class ScheduleUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    task_type: TaskType | None = None
    cron_expression: str | None = Field(default=None, max_length=120)
    timezone: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str | None) -> str | None:
        return None if value is None else _validate_name(value)

    check_task_type = field_validator("task_type")(_validate_task_type)

    @field_validator("cron_expression")
    @classmethod
    def check_cron(cls, value: str | None) -> str | None:
        return None if value is None else validate_cron(value)

    @field_validator("timezone")
    @classmethod
    def check_timezone(cls, value: str | None) -> str | None:
        return None if value is None else _validate_timezone(value)


class ScheduleOut(Timestamps):
    id: int
    name: str
    task_type: TaskType
    cron_expression: str
    timezone: str
    is_active: bool
    last_fired_at: datetime | None
    next_fire_at: datetime | None


class CronValidateRequest(BaseModel):
    cron_expression: str = Field(max_length=120)
    timezone: str = Field(default=DEFAULT_TIMEZONE, max_length=64)


class CronValidateResponse(BaseModel):
    valid: bool
    error: str | None = None
    cron_expression: str | None = Field(default=None, description="Нормализованное выражение")
    next_runs: list[datetime] = Field(default_factory=list, description="5 ближайших запусков, UTC")
