import re

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import Timestamps, clean_str_list

_CATEGORY_RE = re.compile(r"^[a-z][a-z-]*(\.[A-Za-z][A-Za-z-]*)?$")
MAX_QUERY_LENGTH = 1000


def _validate_name(value: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise ValueError("Название не может быть пустым")
    return value


def _validate_queries(values: list[str]) -> list[str]:
    cleaned = clean_str_list(values)
    if not cleaned:
        raise ValueError("Нужен хотя бы один поисковый запрос")
    if any(len(q) > MAX_QUERY_LENGTH for q in cleaned):
        raise ValueError(f"Запрос длиннее {MAX_QUERY_LENGTH} символов")
    return cleaned


def _validate_description(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _validate_categories(values: list[str]) -> list[str]:
    cleaned = clean_str_list(values)
    invalid = [c for c in cleaned if not _CATEGORY_RE.match(c)]
    if invalid:
        raise ValueError(f"Некорректные категории arXiv: {', '.join(invalid)} (пример: cs.CL)")
    return cleaned


class TopicCreate(BaseModel):
    name: str = Field(max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    queries: list[str] = Field(max_length=50)
    arxiv_categories: list[str] = Field(default_factory=list, max_length=50)
    is_active: bool = True

    check_name = field_validator("name")(_validate_name)
    check_description = field_validator("description")(_validate_description)
    check_queries = field_validator("queries")(_validate_queries)
    check_categories = field_validator("arxiv_categories")(_validate_categories)


class TopicUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    queries: list[str] | None = Field(default=None, max_length=50)
    arxiv_categories: list[str] | None = Field(default=None, max_length=50)
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str | None) -> str | None:
        return None if value is None else _validate_name(value)

    check_description = field_validator("description")(_validate_description)

    @field_validator("queries")
    @classmethod
    def check_queries(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _validate_queries(value)

    @field_validator("arxiv_categories")
    @classmethod
    def check_categories(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _validate_categories(value)


class TopicOut(Timestamps):
    id: int
    name: str
    description: str | None
    queries: list[str]
    arxiv_categories: list[str]
    is_active: bool
