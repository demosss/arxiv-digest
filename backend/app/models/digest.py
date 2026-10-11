from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, TimestampMixin
from app.models.enums import DeliveryStatus, DigestContent, DigestStatus, enum_column
from app.models.paper import Paper
from app.models.profile import profile_column


class Digest(TimestampMixin, Base):
    __tablename__ = "digests"
    # один дайджест профиля на период: повторная сборка обновляет ту же запись
    __table_args__ = (UniqueConstraint("profile_id", "period_start", "period_end"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = profile_column()
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    pdf_path: Mapped[str | None] = mapped_column(Text)
    papers_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[DigestStatus] = enum_column(
        DigestStatus, default=DigestStatus.BUILDING, server_default=DigestStatus.BUILDING.value
    )
    error: Mapped[str | None] = mapped_column(Text)
    # состав карточек при последней сборке; дайджесты, собранные до появления аннотаций, — summary
    content: Mapped[DigestContent] = enum_column(
        DigestContent, default=DigestContent.SUMMARY, server_default=DigestContent.SUMMARY.value
    )

    papers: Mapped[list["DigestPaper"]] = relationship(
        back_populates="digest", cascade="all, delete-orphan", order_by="DigestPaper.position"
    )
    deliveries: Mapped[list["DigestDelivery"]] = relationship(
        back_populates="digest", cascade="all, delete-orphan", order_by="DigestDelivery.id"
    )


class DigestPaper(Base):
    """Состав дайджеста и порядок статей."""

    __tablename__ = "digest_papers"

    digest_id: Mapped[int] = mapped_column(
        ForeignKey("digests.id", ondelete="CASCADE"), primary_key=True
    )
    paper_id: Mapped[int] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    # тема, в разделе которой статья выведена полностью
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id", ondelete="SET NULL"))

    digest: Mapped[Digest] = relationship(back_populates="papers")
    paper: Mapped[Paper] = relationship()


class DigestDelivery(TimestampMixin, Base):
    __tablename__ = "digest_deliveries"
    __table_args__ = (UniqueConstraint("digest_id", "email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    digest_id: Mapped[int] = mapped_column(ForeignKey("digests.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(320))
    status: Mapped[DeliveryStatus] = enum_column(
        DeliveryStatus, default=DeliveryStatus.PENDING, server_default=DeliveryStatus.PENDING.value
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    digest: Mapped[Digest] = relationship(back_populates="deliveries")
