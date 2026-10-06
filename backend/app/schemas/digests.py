from datetime import datetime

from pydantic import BaseModel

from app.models.enums import DeliveryStatus, DigestStatus
from app.schemas.common import ORMModel, Timestamps


class DigestOut(Timestamps):
    id: int
    period_start: datetime
    period_end: datetime
    papers_count: int
    status: DigestStatus
    error: str | None
    has_pdf: bool
    filename: str | None


class DigestPaperOut(BaseModel):
    position: int
    paper_id: int
    arxiv_id: str
    title: str
    abs_url: str
    topic_id: int | None
    topic_name: str | None


class DeliveryOut(ORMModel):
    id: int
    email: str
    status: DeliveryStatus
    attempts: int
    error: str | None
    sent_at: datetime | None
    updated_at: datetime


class DigestDetail(DigestOut):
    papers: list[DigestPaperOut]
    deliveries: list[DeliveryOut]


class DigestResendRequest(BaseModel):
    force: bool = True
