"""Шаг collect: поиск препринтов по темам, сохранение, скачивание PDF и извлечение текста."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models import Paper, PaperStatus, PaperTopic, Topic
from app.services.arxiv_client import (
    ArxivEntry,
    ArxivError,
    ArxivUnavailableError,
    pdf_filename,
)
from app.services.arxiv_query import build_search_query, check_query, format_issues
from app.services.pdf_text import ExtractedText, TextExtractionError, extract_text

log = get_logger(__name__)


class ArxivSource(Protocol):
    def search(self, query: str, max_results: int) -> list[ArxivEntry]: ...

    def download_pdf(self, url: str, destination: Path) -> int: ...


@dataclass
class QueryStats:
    found: int = 0
    new: int = 0
    duplicates: int = 0
    errors: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "found": self.found,
            "new": self.new,
            "duplicates": self.duplicates,
            "errors": self.errors,
        }
        if self.error:
            data["error"] = self.error
        return data


@dataclass
class TopicStats:
    """`found` — уникальные статьи темы, `duplicates` — те из них, что уже были в БД."""

    found: int = 0
    new: int = 0
    duplicates: int = 0
    errors: int = 0
    download_errors: int = 0
    zero_result_queries: int = 0
    limit_reached: bool = False
    # нормализованный запрос -> его статистика
    queries: dict[str, QueryStats] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "new": self.new,
            "duplicates": self.duplicates,
            "errors": self.errors,
            "download_errors": self.download_errors,
            "zero_result_queries": self.zero_result_queries,
            "limit_reached": self.limit_reached,
            "queries": {query: stats.as_dict() for query, stats in self.queries.items()},
        }


@dataclass
class CollectStats:
    period_start: datetime
    period_end: datetime
    topics: dict[str, TopicStats] = field(default_factory=dict)
    papers_found: int = 0
    papers_new: int = 0
    duplicates: int = 0
    query_errors: int = 0
    download_errors: int = 0
    text_extracted: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "topics_processed": len(self.topics),
            "papers_found": self.papers_found,
            "papers_new": self.papers_new,
            "duplicates": self.duplicates,
            "query_errors": self.query_errors,
            "download_errors": self.download_errors,
            "text_extracted": self.text_extracted,
            "topics": {name: stats.as_dict() for name, stats in self.topics.items()},
        }


def _needs_processing(paper: Paper) -> bool:
    """Новые статьи и те, чья обработка в прошлый раз оборвалась до извлечения текста."""
    if paper.status in (PaperStatus.NEW, PaperStatus.DOWNLOADED):
        return True
    if paper.status == PaperStatus.IRRELEVANT:
        # отброшенную статью нашла ещё одна тема: текст нужен снова, чтобы оценить её для этой темы
        return any(link.relevance is None for link in paper.topic_links)
    return paper.status == PaperStatus.FAILED and paper.text_chars is None


class Collector:
    def __init__(
        self,
        session: Session,
        source: ArxivSource,
        *,
        papers_dir: Path,
        max_results_per_topic: int,
        text_extractor: Callable[[Path], ExtractedText] = extract_text,
    ) -> None:
        self._session = session
        self._source = source
        self._papers_dir = papers_dir
        self._max_results = max_results_per_topic
        self._extract_text = text_extractor

    def run(self, period_start: datetime, period_end: datetime) -> CollectStats:
        stats = CollectStats(period_start=period_start, period_end=period_end)
        topics = self._session.scalars(
            select(Topic).where(Topic.is_active.is_(True)).order_by(Topic.id)
        ).all()
        if not topics:
            log.warning("collect_no_active_topics")

        # paper_id -> имена тем, по которым статья найдена в этом запуске
        found: dict[int, list[str]] = {}
        for topic in topics:
            topic_stats = stats.topics.setdefault(topic.name, TopicStats())
            for paper_id in self._collect_topic(topic, period_start, period_end, topic_stats):
                found.setdefault(paper_id, []).append(topic.name)

        stats.papers_found = len(found)
        stats.papers_new = sum(t.new for t in stats.topics.values())
        stats.duplicates = sum(t.duplicates for t in stats.topics.values())
        stats.query_errors = sum(t.errors for t in stats.topics.values())

        for paper_id, topic_names in found.items():
            paper = self._session.get_one(Paper, paper_id)
            if not _needs_processing(paper):
                continue
            if self._process_paper(paper):
                stats.text_extracted += 1
            else:
                stats.download_errors += 1
                for name in topic_names:
                    stats.topics[name].download_errors += 1

        log.info(
            "collect_done",
            papers_found=stats.papers_found,
            papers_new=stats.papers_new,
            duplicates=stats.duplicates,
            query_errors=stats.query_errors,
            download_errors=stats.download_errors,
            text_extracted=stats.text_extracted,
        )
        return stats

    def _collect_topic(
        self, topic: Topic, period_start: datetime, period_end: datetime, stats: TopicStats
    ) -> list[int]:
        """Выполняет запросы темы по одному; лимит результатов действует на тему в целом."""
        log.info(
            "collect_topic_started", topic=topic.name, topic_id=topic.id, queries=len(topic.queries)
        )
        # paper_id -> запросы темы, которые нашли статью (в порядке срабатывания)
        matched: dict[int, list[str]] = {}
        zero_results: list[str] = []

        for raw_query in topic.queries:
            check = check_query(raw_query)
            key = check.normalized or raw_query
            if key in stats.queries:
                continue
            query_stats = stats.queries.setdefault(key, QueryStats())

            if not check.ok:
                query_stats.errors = 1
                query_stats.error = f"Некорректный запрос: {format_issues(check.errors)}"
                log.error(
                    "collect_query_invalid",
                    topic=topic.name,
                    query=raw_query,
                    error=query_stats.error,
                )
                continue

            remaining = self._max_results - len(matched)
            if remaining <= 0:
                query_stats.error = "Пропущен: достигнут лимит результатов на тему"
                continue

            search_query = build_search_query(
                check.normalized, topic.arxiv_categories, period_start, period_end
            )
            log.info(
                "collect_query_started",
                topic=topic.name,
                query=check.normalized,
                search_query=search_query,
            )
            try:
                entries = self._source.search(search_query, remaining)
            except ArxivUnavailableError:
                # arXiv недоступен целиком: шаг повторит Celery
                raise
            except ArxivError as exc:
                query_stats.errors = 1
                query_stats.error = str(exc)
                log.error(
                    "collect_query_failed",
                    topic=topic.name,
                    query=check.normalized,
                    error=str(exc),
                )
                continue

            for entry in entries:
                paper, created = self._get_or_create_paper(entry)
                if created:
                    query_stats.new += 1
                else:
                    # статья уже известна: по другому запросу, другой теме или из прошлого запуска
                    query_stats.duplicates += 1
                matched.setdefault(paper.id, []).append(check.normalized)
            query_stats.found = len(entries)
            if not entries:
                zero_results.append(check.normalized)
            log.info(
                "collect_query_done",
                topic=topic.name,
                query=check.normalized,
                found=query_stats.found,
                new=query_stats.new,
                duplicates=query_stats.duplicates,
            )

            if len(matched) >= self._max_results and not stats.limit_reached:
                stats.limit_reached = True
                log.warning(
                    "collect_topic_limit_reached", topic=topic.name, limit=self._max_results
                )

        for paper_id, queries in matched.items():
            link = self._session.get(PaperTopic, (paper_id, topic.id))
            if link is None:
                self._session.add(
                    PaperTopic(paper_id=paper_id, topic_id=topic.id, matched_queries=queries)
                )
            else:
                # накапливаем: статья могла быть найдена другими запросами в прошлых запусках
                link.matched_queries = list(dict.fromkeys([*link.matched_queries, *queries]))
        self._session.commit()

        stats.found = len(matched)
        stats.new = sum(q.new for q in stats.queries.values())
        stats.duplicates = stats.found - stats.new
        stats.errors = sum(q.errors for q in stats.queries.values())
        stats.zero_result_queries = len(zero_results)
        log.info(
            "collect_topic_done",
            topic=topic.name,
            found=stats.found,
            new=stats.new,
            duplicates=stats.duplicates,
            errors=stats.errors,
            limit_reached=stats.limit_reached,
        )
        if zero_results:
            log.info("collect_topic_zero_results", topic=topic.name, queries=zero_results)
        return list(matched)

    def _get_or_create_paper(self, entry: ArxivEntry) -> tuple[Paper, bool]:
        paper = self._session.scalar(select(Paper).where(Paper.arxiv_id == entry.arxiv_id))
        if paper is not None:
            return paper, False
        paper = Paper(
            arxiv_id=entry.arxiv_id,
            version=entry.version,
            title=entry.title,
            authors=entry.authors,
            abstract=entry.abstract,
            categories=entry.categories,
            published_at=entry.published_at,
            updated_at_arxiv=entry.updated_at,
            pdf_url=entry.pdf_url,
            abs_url=entry.abs_url,
            status=PaperStatus.NEW,
        )
        self._session.add(paper)
        self._session.flush()
        return paper, True

    def _process_paper(self, paper: Paper) -> bool:
        """Скачивает PDF и извлекает текст. Ошибка одной статьи не прерывает сбор."""
        with structlog.contextvars.bound_contextvars(paper_id=paper.id, arxiv_id=paper.arxiv_id):
            destination = self._papers_dir / pdf_filename(paper.arxiv_id)
            try:
                if not (paper.status == PaperStatus.DOWNLOADED and destination.is_file()):
                    self._source.download_pdf(paper.pdf_url, destination)
                    paper.pdf_path = str(destination)
                    paper.status = PaperStatus.DOWNLOADED
                    paper.error = None
                    self._session.commit()

                extracted = self._extract_text(destination)
                paper.full_text = extracted.text
                paper.text_chars = len(extracted.text)
                paper.status = PaperStatus.TEXT_EXTRACTED
                paper.error = None
                self._session.commit()
                return True
            except (ArxivError, TextExtractionError, OSError) as exc:
                self._session.rollback()
                paper.status = PaperStatus.FAILED
                paper.error = str(exc)
                self._session.commit()
                log.error("paper_processing_failed", error=str(exc))
                return False
