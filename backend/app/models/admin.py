from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, true
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin
from app.models.enums import SmtpSecurity, TaskType, enum_column
from app.models.profile import profile_column

SMTP_SETTINGS_ID = 1


class AdminUser(TimestampMixin, Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(150), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


class Topic(TimestampMixin, Base):
    __tablename__ = "topics"
    # название темы уникально в пределах профиля
    __table_args__ = (UniqueConstraint("profile_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = profile_column()
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    # поисковые выражения arXiv API; каждое выполняется отдельным запросом
    queries: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    arxiv_categories: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


class Recipient(TimestampMixin, Base):
    __tablename__ = "recipients"
    # один адрес может получать дайджесты нескольких профилей
    __table_args__ = (UniqueConstraint("profile_id", "email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = profile_column()
    email: Mapped[str] = mapped_column(String(320))
    name: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


class Schedule(TimestampMixin, Base):
    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = profile_column(index=True)
    name: Mapped[str] = mapped_column(String(255))
    task_type: Mapped[TaskType] = enum_column(TaskType)
    cron_expression: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(
        String(64), default="Europe/Moscow", server_default="Europe/Moscow"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SmtpSettings(TimestampMixin, Base):
    """Singleton: единственная строка с id=1. Пароль в БД не хранится (только SMTP_PASSWORD)."""

    __tablename__ = "smtp_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    host: Mapped[str] = mapped_column(String(255), default="", server_default="")
    port: Mapped[int] = mapped_column(Integer, default=587, server_default="587")
    username: Mapped[str] = mapped_column(String(255), default="", server_default="")
    from_email: Mapped[str] = mapped_column(String(320), default="", server_default="")
    from_name: Mapped[str] = mapped_column(String(255), default="", server_default="")
    security: Mapped[SmtpSecurity] = enum_column(
        SmtpSecurity, default=SmtpSecurity.STARTTLS, server_default=SmtpSecurity.STARTTLS.value
    )
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
