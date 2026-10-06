import re

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import Timestamps, clean_str_list

_CATEGORY_RE = re.compile(r"^[a-z][a-z-]*(\.[A-Za-z][A-Za-z-]*)?$")
MAX_KEYWORD_LENGTH = 200


def _validate_name(value: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise ValueError("Название не может быть пустым")
    return value


def _validate_keywords(values: list[str]) -> list[str]:
    # кавычки ломают поисковый запрос arXiv: фразы оборачиваются в них автоматически
    cleaned = clean_str_list([v.replace('"', " ") for v in values])
    if not cleaned:
        raise ValueError("Нужно хотя бы одно ключевое слово")
    too_long = [k for k in cleaned if len(k) > MAX_KEYWORD_LENGTH]
    if too_long:
        raise ValueError(f"Ключевое слово длиннее {MAX_KEYWORD_LENGTH} символов")
    return cleaned


def _validate_categories(values: list[str]) -> list[str]:
    cleaned = clean_str_list(values)
    invalid = [c for c in cleaned if not _CATEGORY_RE.match(c)]
    if invalid:
        raise ValueError(f"Некорректные категории arXiv: {', '.join(invalid)} (пример: cs.CL)")
    return cleaned


class TopicCreate(BaseModel):
    name: str = Field(max_length=255)
    keywords: list[str] = Field(max_length=50)
    arxiv_categories: list[str] = Field(default_factory=list, max_length=50)
    is_active: bool = True

    check_name = field_validator("name")(_validate_name)
    check_keywords = field_validator("keywords")(_validate_keywords)
    check_categories = field_validator("arxiv_categories")(_validate_categories)


class TopicUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    keywords: list[str] | None = Field(default=None, max_length=50)
    arxiv_categories: list[str] | None = Field(default=None, max_length=50)
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str | None) -> str | None:
        return None if value is None else _validate_name(value)

    @field_validator("keywords")
    @classmethod
    def check_keywords(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _validate_keywords(value)

    @field_validator("arxiv_categories")
    @classmethod
    def check_categories(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _validate_categories(value)


class TopicOut(Timestamps):
    id: int
    name: str
    keywords: list[str]
    arxiv_categories: list[str]
    is_active: bool
