from datetime import datetime

from pydantic import BaseModel

from app.models.enums import PaperStatus, Relevance
from app.schemas.common import ORMModel, Timestamps
from app.schemas.translations import TranslationOut


class PaperTopicOut(BaseModel):
    id: int
    name: str
    matched_keywords: list[str]
    matched_queries: list[str]
    relevance: Relevance | None
    relevance_reason: str | None


class SummaryOut(Timestamps):
    text_ru: str
    abstract_ru: str | None
    model: str
    prompt_tokens: int
    completion_tokens: int
    duration_ms: int
    chunks_count: int


class PaperOut(ORMModel):
    id: int
    arxiv_id: str
    version: int
    title: str
    authors: list[str]
    categories: list[str]
    published_at: datetime
    abs_url: str
    pdf_url: str
    status: PaperStatus
    error: str | None
    text_chars: int | None
    created_at: datetime
    topics: list[PaperTopicOut]
    has_summary: bool


class PaperDetail(PaperOut):
    abstract: str
    updated_at_arxiv: datetime | None
    updated_at: datetime
    summary: SummaryOut | None
    translations: list[TranslationOut]


class LLMHealthOut(BaseModel):
    ok: bool
    base_url: str
    model: str
    latency_ms: int | None
    model_available: bool | None
    models: list[str]
    error: str | None
