from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, TimestampMixin
from app.models.enums import TranslationStatus, enum_column
from app.models.paper import Paper


class Translation(TimestampMixin, Base):
    """Перевод статьи в docx. Один перевод на язык: повторный заменяет файл."""

    __tablename__ = "translations"
    __table_args__ = (UniqueConstraint("paper_id", "language"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    # код языка из списка (ru, en, ...) либо название, введённое вручную
    language: Mapped[str] = mapped_column(String(64))
    status: Mapped[TranslationStatus] = enum_column(
        TranslationStatus,
        default=TranslationStatus.QUEUED,
        server_default=TranslationStatus.QUEUED.value,
    )
    # откуда взята структура статьи: html (версия arXiv) или pdf
    source: Mapped[str | None] = mapped_column(String(16))
    docx_path: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(255))
    # запуск, который выполняет или выполнил перевод: по нему в UI открываются логи
    run_id: Mapped[int | None] = mapped_column(ForeignKey("task_runs.id", ondelete="SET NULL"))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    paper: Mapped[Paper] = relationship()
