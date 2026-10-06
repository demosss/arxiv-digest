from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, TimestampMixin, utcnow
from app.models.enums import RunStatus, RunTrigger, TaskType, enum_column


class TaskRun(TimestampMixin, Base):
    """Запуск задачи или шага пайплайна (шаги ссылаются на родителя через parent_run_id)."""

    __tablename__ = "task_runs"
    __table_args__ = (
        Index("ix_task_runs_task_type_status", "task_type", "status"),
        Index("ix_task_runs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_type: Mapped[TaskType] = enum_column(TaskType)
    trigger: Mapped[RunTrigger] = enum_column(RunTrigger)
    schedule_id: Mapped[int | None] = mapped_column(ForeignKey("schedules.id", ondelete="SET NULL"))
    parent_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("task_runs.id", ondelete="CASCADE"), index=True
    )
    celery_task_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[RunStatus] = enum_column(
        RunStatus, default=RunStatus.QUEUED, server_default=RunStatus.QUEUED.value
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    # входные параметры запуска: период, digest_id, paper_ids, force
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    error: Mapped[str | None] = mapped_column(Text)
    traceback: Mapped[str | None] = mapped_column(Text)

    children: Mapped[list["TaskRun"]] = relationship(
        back_populates="parent", cascade="all, delete-orphan", order_by="TaskRun.id"
    )
    parent: Mapped["TaskRun | None"] = relationship(back_populates="children", remote_side=[id])


class TaskLog(Base):
    __tablename__ = "task_logs"
    __table_args__ = (Index("ix_task_logs_run_id_ts", "run_id", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    level: Mapped[str] = mapped_column(String(16))
    logger: Mapped[str] = mapped_column(String(255), default="", server_default="")
    message: Mapped[str] = mapped_column(Text)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
