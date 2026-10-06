import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from app.core.logging import get_logger
from app.schemas.common import Timestamps, clean_str_list
from app.services.arxiv_query import check_query, format_issues

log = get_logger(__name__)

_CATEGORY_RE = re.compile(r"^[a-z][a-z-]*(\.[A-Za-z][A-Za-z-]*)?$")
MAX_QUERIES = 50
# тип ошибки запроса в ответе 422; для него обработчик ошибок добавляет поле position
INVALID_QUERY_TYPE = "invalid_query"


def _validate_name(value: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise ValueError("Название не может быть пустым")
    return value


def _validate_description(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _validate_query(value: str) -> str:
    """Нормализует запрос; при ошибках синтаксиса — 422 с позицией первой из них."""
    check = check_query(value)
    if check.errors:
        raise PydanticCustomError(
            INVALID_QUERY_TYPE,
            "{message}",
            {"message": format_issues(check.errors), "position": check.errors[0].position},
        )
    return check.normalized


Query = Annotated[str, AfterValidator(_validate_query)]


def _drop_blank(values: Any) -> Any:
    if isinstance(values, list):
        return [v for v in values if not (isinstance(v, str) and not v.strip())]
    return values


def _validate_queries(values: list[str]) -> list[str]:
    cleaned = clean_str_list(values)
    if not cleaned:
        raise ValueError("Нужен хотя бы один поисковый запрос")
    return cleaned


def _validate_categories(values: list[str]) -> list[str]:
    cleaned = clean_str_list(values)
    invalid = [c for c in cleaned if not _CATEGORY_RE.match(c)]
    if invalid:
        raise ValueError(f"Некорректные категории arXiv: {', '.join(invalid)} (пример: cs.CL)")
    return cleaned


def keyword_to_query(keyword: str) -> str:
    phrase = " ".join(keyword.replace('"', " ").split())
    return f'all:"{phrase}"'


def _convert_legacy_keywords(data: Any) -> Any:
    """DEPRECATED: старые клиенты присылают `keywords`; превращаем их в запросы `all:"..."`."""
    if not isinstance(data, dict) or "keywords" not in data:
        return data
    data = dict(data)
    keywords = data.pop("keywords")
    if data.get("queries") is None and isinstance(keywords, list):
        log.warning("topic_keywords_deprecated", keywords=len(keywords))
        data["queries"] = [
            keyword_to_query(k) for k in keywords if isinstance(k, str) and k.strip(' "')
        ]
    return data


class TopicCreate(BaseModel):
    name: str = Field(max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    queries: list[Query] = Field(
        max_length=MAX_QUERIES,
        description="Поисковые выражения arXiv API; строка без префиксов считается фразой",
    )
    arxiv_categories: list[str] = Field(default_factory=list, max_length=50)
    is_active: bool = True

    convert_keywords = model_validator(mode="before")(_convert_legacy_keywords)
    drop_blank_queries = field_validator("queries", mode="before")(_drop_blank)
    check_name = field_validator("name")(_validate_name)
    check_description = field_validator("description")(_validate_description)
    check_queries = field_validator("queries")(_validate_queries)
    check_categories = field_validator("arxiv_categories")(_validate_categories)


class TopicUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    queries: list[Query] | None = Field(default=None, max_length=MAX_QUERIES)
    arxiv_categories: list[str] | None = Field(default=None, max_length=50)
    is_active: bool | None = None

    convert_keywords = model_validator(mode="before")(_convert_legacy_keywords)
    drop_blank_queries = field_validator("queries", mode="before")(_drop_blank)
    check_description = field_validator("description")(_validate_description)

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str | None) -> str | None:
        return None if value is None else _validate_name(value)

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


# --- проверка запроса ---


class QueryValidateRequest(BaseModel):
    query: str = Field(max_length=5000)


class QueryIssueOut(BaseModel):
    position: int = Field(description="Индекс символа (с нуля) в нормализованном запросе")
    message: str


class QueryValidateResponse(BaseModel):
    normalized: str
    errors: list[QueryIssueOut]
    warnings: list[QueryIssueOut]


# --- предпросмотр ---


class TopicPreviewRequest(BaseModel):
    queries: list[Query] = Field(max_length=MAX_QUERIES)
    categories: list[str] = Field(default_factory=list, max_length=50)
    days: int = Field(default=7, ge=1, le=30)

    drop_blank_queries = field_validator("queries", mode="before")(_drop_blank)
    check_queries = field_validator("queries")(_validate_queries)
    check_categories = field_validator("categories")(_validate_categories)


class PreviewExample(BaseModel):
    arxiv_id: str
    title: str
    published_at: datetime
    abs_url: str


class PreviewQueryResult(BaseModel):
    query: str
    total: int = Field(description="totalResults из ответа arXiv")
    examples: list[PreviewExample]
    error: str | None = None


class TopicPreviewResponse(BaseModel):
    days: int
    period_start: datetime
    period_end: datetime
    queries: list[PreviewQueryResult]
    unique_total: int = Field(description="Уникальные статьи среди полученных по всем запросам")
    unique_is_lower_bound: bool = Field(
        description="true, если по какому-то запросу результатов больше, чем было загружено"
    )


# --- импорт и экспорт ---


class ImportIssue(BaseModel):
    loc: list[str | int]
    msg: str
    type: str = "import_error"
    position: int | None = None
    topic: str | None = None


class TopicImportReport(BaseModel):
    mode: Literal["merge", "replace"]
    dry_run: bool
    applied: bool
    created: list[str]
    updated: list[str]
    unchanged: list[str]
    deleted: list[str]
    errors: list[ImportIssue]
