"""Предпросмотр темы: что запросы находят на arXiv за последние дни, без записи в БД."""

import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

import httpx

from app.core.config import get_settings
from app.core.db import utcnow
from app.core.errors import RetryableError, TooManyRequestsError
from app.core.logging import get_logger
from app.services.arxiv_client import PAGE_SIZE, ArxivClient, ArxivEntry, ArxivError
from app.services.arxiv_query import build_search_query

log = get_logger(__name__)

MAX_EXAMPLES = 10
# preview ходит в arXiv из процесса API: одновременно выполняется не больше одного
_preview_lock = threading.Lock()


class PreviewSource(Protocol):
    def search_page(self, query: str, max_results: int) -> tuple[list[ArxivEntry], int]: ...


@dataclass
class QueryPreview:
    query: str
    total: int = 0
    examples: list[ArxivEntry] = field(default_factory=list)
    error: str | None = None


@dataclass
class TopicPreview:
    days: int
    period_start: datetime
    period_end: datetime
    queries: list[QueryPreview]
    unique_total: int
    unique_is_lower_bound: bool


@contextmanager
def arxiv_source() -> Iterator[PreviewSource]:
    settings = get_settings()
    with httpx.Client() as http:
        yield ArxivClient(
            http,
            request_delay=settings.arxiv_request_delay_seconds,
            user_agent=settings.arxiv_user_agent,
        )


def preview_topic(
    queries: list[str],
    categories: list[str],
    days: int,
    *,
    source_factory: Callable[[], AbstractContextManager[PreviewSource]] | None = None,
) -> TopicPreview:
    """По одному запросу к arXiv на каждый запрос темы (первая страница), с обычными паузами."""
    if not _preview_lock.acquire(blocking=False):
        raise TooManyRequestsError(
            "Предпросмотр уже выполняется: дождитесь его завершения", code="preview_busy"
        )
    try:
        period_end = utcnow()
        period_start = period_end - timedelta(days=days)
        results: list[QueryPreview] = []
        seen: set[str] = set()
        lower_bound = False
        unavailable: str | None = None

        with (source_factory or arxiv_source)() as source:
            for query in queries:
                item = QueryPreview(query=query)
                results.append(item)
                if unavailable is not None:
                    item.error = unavailable
                    continue
                search_query = build_search_query(query, categories, period_start, period_end)
                try:
                    entries, total = source.search_page(search_query, PAGE_SIZE)
                except ArxivError as exc:
                    item.error = str(exc)
                    log.warning("topic_preview_query_failed", query=query, error=str(exc))
                    if isinstance(exc, RetryableError):
                        # arXiv недоступен: остальные запросы не мучаем ретраями
                        unavailable = str(exc)
                    continue
                item.total = total
                item.examples = entries[:MAX_EXAMPLES]
                seen.update(entry.arxiv_id for entry in entries)
                lower_bound = lower_bound or total > len(entries)

        log.info(
            "topic_preview_done",
            queries=len(queries),
            categories=categories,
            days=days,
            unique_total=len(seen),
            totals=[item.total for item in results],
            errors=sum(1 for item in results if item.error),
        )
        return TopicPreview(
            days=days,
            period_start=period_start,
            period_end=period_end,
            queries=results,
            unique_total=len(seen),
            unique_is_lower_bound=lower_bound,
        )
    finally:
        _preview_lock.release()
