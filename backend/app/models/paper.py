from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, TimestampMixin
from app.models.admin import Topic
from app.models.enums import PaperStatus, Relevance, enum_column
from app.models.profile import profile_column


class Paper(TimestampMixin, Base):
    __tablename__ = "papers"
    __table_args__ = (
        # у каждого профиля своя запись статьи: статус, темы и переводы не общие
        UniqueConstraint("profile_id", "arxiv_id"),
        Index("ix_papers_published_at", "published_at"),
        Index("ix_papers_status", "status"),
        Index("ix_papers_arxiv_id", "arxiv_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = profile_column()
    # идентификатор без версии, например 2410.01234
    arxiv_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    title: Mapped[str] = mapped_column(Text)
    authors: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    abstract: Mapped[str] = mapped_column(Text, default="", server_default="")
    categories: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at_arxiv: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pdf_url: Mapped[str] = mapped_column(Text)
    abs_url: Mapped[str] = mapped_column(Text)
    pdf_path: Mapped[str | None] = mapped_column(Text)
    full_text: Mapped[str | None] = mapped_column(Text, deferred=True)
    text_chars: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[PaperStatus] = enum_column(
        PaperStatus, default=PaperStatus.NEW, server_default=PaperStatus.NEW.value
    )
    error: Mapped[str | None] = mapped_column(Text)

    topic_links: Mapped[list["PaperTopic"]] = relationship(
        back_populates="paper", cascade="all, delete-orphan"
    )
    summary: Mapped["Summary | None"] = relationship(
        back_populates="paper", cascade="all, delete-orphan", uselist=False
    )


class PaperTopic(Base):
    """Какая тема и какими запросами нашла статью."""

    __tablename__ = "paper_topics"

    paper_id: Mapped[int] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True
    )
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    # устаревшее: заполнялось, пока темы задавались ключевыми словами
    matched_keywords: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}"
    )
    matched_queries: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}"
    )
    # оценка LLM по названию и аннотации; NULL — не проверялась
    relevance: Mapped[Relevance | None] = enum_column(Relevance, nullable=True)
    relevance_reason: Mapped[str | None] = mapped_column(Text)

    paper: Mapped[Paper] = relationship(back_populates="topic_links")
    topic: Mapped[Topic] = relationship()


class Summary(TimestampMixin, Base):
    __tablename__ = "summaries"

    id: Mapped[int] = mapped_column(primary_key=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), unique=True)
    text_ru: Mapped[str] = mapped_column(Text)
    # краткая аннотация одним абзацем; NULL — резюме создано до её появления
    abstract_ru: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(255))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    chunks_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    paper: Mapped[Paper] = relationship(back_populates="summary")
