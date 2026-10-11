from pydantic import BaseModel, Field, field_validator

from app.models.enums import TranslationStatus
from app.schemas.common import Timestamps
from app.services.translation.languages import MAX_LANGUAGE_LENGTH, normalize_language


class TranslateRequest(BaseModel):
    language: str = Field(
        min_length=1,
        max_length=MAX_LANGUAGE_LENGTH,
        description="Код языка из списка (ru, en, de, fr, es) или название, введённое вручную",
    )

    check_language = field_validator("language")(normalize_language)


class LanguageOut(BaseModel):
    code: str
    label: str


class TranslationOut(Timestamps):
    id: int
    paper_id: int
    language: str
    language_label: str
    status: TranslationStatus
    # откуда взята структура статьи: html или pdf
    source: str | None
    error: str | None
    model: str | None
    run_id: int | None
    duration_ms: int
    has_file: bool
    filename: str | None


class TranslationListItem(TranslationOut):
    arxiv_id: str
    title: str
