"""Клиент arXiv: поиск через Atom API и скачивание PDF."""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import feedparser
import httpx

from app.core.errors import RetryableError
from app.core.logging import get_logger

log = get_logger(__name__)

# http://export.arxiv.org/api/query отвечает 301 на https, поэтому сразу https
ARXIV_API_URL = "https://export.arxiv.org/api/query"
PAGE_SIZE = 100
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 5.0
# 429 и 503 — arXiv перегружен; за 5–20 с такая перегрузка не проходит
OVERLOAD_BACKOFF_BASE_SECONDS = 30.0
OVERLOAD_STATUSES = frozenset({429, 503})
# Retry-After больше этого значения не ждём, чтобы не держать воркер
MAX_RETRY_AFTER_SECONDS = 300.0
# arXiv иногда отдаёт пустую страницу при непустой выдаче — такой ответ перезапрашивается
MAX_EMPTY_PAGE_RETRIES = 2
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
HTTP_TIMEOUT = httpx.Timeout(60.0, connect=15.0)

_ARXIV_ID_RE = re.compile(r"arxiv\.org/abs/(?P<id>.+?)(?:v(?P<version>\d+))?$")


class ArxivError(Exception):
    """Ошибка arXiv, которую повтор не исправит (некорректный запрос, нераспознанный ответ)."""


class ArxivUnavailableError(ArxivError, RetryableError):
    """arXiv недоступен после всех попыток: сетевые ошибки, 5xx, rate limit."""

    # перегрузка arXiv длится минутами: шаг повторяется через 5, 10 и 20 минут
    retry_backoff_base = 300


@dataclass(frozen=True, slots=True)
class ArxivEntry:
    arxiv_id: str
    version: int
    title: str
    authors: list[str]
    abstract: str
    categories: list[str]
    published_at: datetime
    updated_at: datetime | None
    pdf_url: str
    abs_url: str


def build_query_url(search_query: str, start: int, max_results: int) -> str:
    """Полный URL запроса к arXiv API; `httpx` кодирует пробелы, кавычки, скобки и двоеточия."""
    return str(httpx.URL(ARXIV_API_URL, params=_query_params(search_query, start, max_results)))


def _query_params(search_query: str, start: int, max_results: int) -> dict[str, str | int]:
    return {
        "search_query": search_query,
        "start": start,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_entry(raw: Any) -> ArxivEntry:
    match = _ARXIV_ID_RE.search(raw.get("id", ""))
    if match is None:
        raise ArxivError(f"Не удалось разобрать идентификатор статьи: {raw.get('id')!r}")
    arxiv_id = match.group("id")
    version = int(match.group("version") or 1)
    published = _parse_datetime(raw.get("published"))
    if published is None:
        raise ArxivError(f"У статьи {arxiv_id} нет даты публикации")

    pdf_url = next(
        (
            link["href"]
            for link in raw.get("links", [])
            if link.get("title") == "pdf" or link.get("type") == "application/pdf"
        ),
        f"https://arxiv.org/pdf/{arxiv_id}v{version}",
    )
    return ArxivEntry(
        arxiv_id=arxiv_id,
        version=version,
        title=_collapse(raw.get("title", "")),
        authors=[_collapse(a.get("name", "")) for a in raw.get("authors", []) if a.get("name")],
        abstract=_collapse(raw.get("summary", "")),
        categories=[t["term"] for t in raw.get("tags", []) if t.get("term")],
        published_at=published,
        updated_at=_parse_datetime(raw.get("updated")),
        pdf_url=pdf_url.replace("http://", "https://", 1),
        abs_url=f"https://arxiv.org/abs/{arxiv_id}",
    )


def parse_feed(content: str | bytes) -> tuple[list[ArxivEntry], int]:
    """Разбирает Atom-ответ arXiv. Возвращает статьи страницы и общее число результатов."""
    feed = feedparser.parse(content)
    if feed.bozo and not feed.entries and "opensearch_totalresults" not in feed.feed:
        raise ArxivError(f"Некорректный ответ arXiv API: {feed.bozo_exception}")
    for raw in feed.entries:
        # ошибки запроса arXiv возвращает как обычный entry с id .../api/errors#...
        if "/api/errors" in raw.get("id", ""):
            raise ArxivError(f"arXiv API вернул ошибку: {_collapse(raw.get('summary', ''))}")
    entries = [_parse_entry(raw) for raw in feed.entries]
    total = int(feed.feed.get("opensearch_totalresults", len(entries)))
    return entries, total


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Пауза из заголовка Retry-After (секунды или HTTP-дата); None — заголовка нет или он битый."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - (now or datetime.now(UTC))).total_seconds())


def pdf_filename(arxiv_id: str) -> str:
    """Имя файла для статьи; в старых идентификаторах (hep-th/9901001) есть слеш."""
    return f"{arxiv_id.replace('/', '_')}.pdf"


class ArxivClient:
    def __init__(
        self,
        http: httpx.Client,
        *,
        request_delay: float,
        user_agent: str,
        sleep: Callable[[float], None] = time.sleep,
        backoff_base: float = BACKOFF_BASE_SECONDS,
        overload_backoff_base: float = OVERLOAD_BACKOFF_BASE_SECONDS,
    ) -> None:
        self._http = http
        self._delay = request_delay
        self._headers = {"User-Agent": user_agent}
        self._sleep = sleep
        self._backoff_base = backoff_base
        self._overload_backoff_base = overload_backoff_base
        self._last_request_at: float | None = None

    def _throttle(self) -> None:
        """Выдерживает паузу между любыми запросами к arXiv (API и PDF)."""
        if self._last_request_at is not None:
            wait = self._delay - (time.monotonic() - self._last_request_at)
            if wait > 0:
                log.debug("arxiv_throttle", delay_seconds=round(wait, 2))
                self._sleep(wait)
        self._last_request_at = time.monotonic()

    def _with_retries[T](self, what: str, url: str, action: Callable[[], T]) -> T:
        last_error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._throttle()
            base = self._backoff_base
            retry_after: float | None = None
            try:
                return action()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                last_error = f"HTTP {status}"
                if status not in RETRYABLE_STATUSES:
                    log.error("arxiv_http_error", what=what, url=url, status=status)
                    raise ArxivError(f"{what}: {last_error} ({url})") from exc
                if status in OVERLOAD_STATUSES:
                    base = self._overload_backoff_base
                retry_after = parse_retry_after(exc.response.headers.get("Retry-After"))
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < MAX_ATTEMPTS:
                backoff = base * 2 ** (attempt - 1)
                if retry_after is not None:
                    backoff = max(backoff, min(retry_after, MAX_RETRY_AFTER_SECONDS))
                log.warning(
                    "arxiv_retry",
                    what=what,
                    url=url,
                    attempt=attempt,
                    error=last_error,
                    backoff_seconds=backoff,
                    retry_after=retry_after,
                )
                self._sleep(backoff)
        log.error("arxiv_unavailable", what=what, url=url, attempts=MAX_ATTEMPTS, error=last_error)
        raise ArxivUnavailableError(f"{what}: {last_error} после {MAX_ATTEMPTS} попыток ({url})")

    def _fetch_page(self, query: str, start: int, size: int) -> tuple[list[ArxivEntry], int]:
        params = _query_params(query, start, size)
        url = build_query_url(query, start, size)
        log.debug("arxiv_request", url=url)

        def action() -> tuple[list[ArxivEntry], int]:
            response = self._http.get(
                ARXIV_API_URL,
                params=params,
                headers=self._headers,
                timeout=HTTP_TIMEOUT,
                follow_redirects=True,
            )
            response.raise_for_status()
            return parse_feed(response.content)

        entries, total = self._with_retries("Запрос к arXiv API", url, action)
        log.info("arxiv_page", query=query, start=start, received=len(entries), total=total)
        return entries, total

    def search_page(self, query: str, max_results: int) -> tuple[list[ArxivEntry], int]:
        """Первая страница выдачи и общее число результатов (`totalResults`)."""
        return self._fetch_page(query, 0, min(PAGE_SIZE, max_results))

    def search(self, query: str, max_results: int) -> list[ArxivEntry]:
        """Все результаты запроса (не больше `max_results`), постранично по PAGE_SIZE."""
        results: list[ArxivEntry] = []
        start = 0
        empty_retries = 0
        while start < max_results:
            entries, total = self._fetch_page(query, start, min(PAGE_SIZE, max_results - start))
            if not entries:
                if start < total and empty_retries < MAX_EMPTY_PAGE_RETRIES:
                    empty_retries += 1
                    log.warning("arxiv_empty_page", start=start, total=total, retry=empty_retries)
                    continue
                break
            empty_retries = 0
            results.extend(entries)
            start += len(entries)
            if start >= total:
                break
        if start < max_results:
            log.info("arxiv_search_done", results=len(results))
        else:
            log.warning("arxiv_search_truncated", results=len(results), max_results=max_results)
        return results[:max_results]

    def fetch(self, url: str, what: str = "Загрузка страницы") -> tuple[bytes, str]:
        """Содержимое страницы или файла с arXiv и конечный адрес после редиректов."""

        def action() -> tuple[bytes, str]:
            response = self._http.get(
                url, headers=self._headers, timeout=HTTP_TIMEOUT, follow_redirects=True
            )
            response.raise_for_status()
            return response.content, str(response.url)

        return self._with_retries(what, url, action)

    def download_pdf(self, url: str, destination: Path) -> int:
        """Скачивает PDF через временный файл. Возвращает размер в байтах."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_path = destination.with_suffix(destination.suffix + ".part")

        def action() -> int:
            size = 0
            with self._http.stream(
                "GET", url, headers=self._headers, timeout=HTTP_TIMEOUT, follow_redirects=True
            ) as response:
                response.raise_for_status()
                with temp_path.open("wb") as file:
                    for chunk in response.iter_bytes():
                        file.write(chunk)
                        size += len(chunk)
            return size

        started = time.perf_counter()
        try:
            size = self._with_retries("Скачивание PDF", url, action)
            with temp_path.open("rb") as file:
                if file.read(5) != b"%PDF-":
                    raise ArxivError(f"Скачивание PDF: по адресу {url} отдан не PDF")
            temp_path.replace(destination)
        finally:
            temp_path.unlink(missing_ok=True)
        log.info(
            "pdf_downloaded",
            url=url,
            size_bytes=size,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
        return size
