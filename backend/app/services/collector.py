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
    build_search_query,
    match_keywords,
    pdf_filename,
)
from app.services.pdf_text import ExtractedText, TextExtractionError, extract_text

log = get_logger(__name__)


class ArxivSource(Protocol):
    def search(self, query: str, max_results: int) -> list[ArxivEntry]: ...

    def download_pdf(self, url: str, destination: Path) -> int: ...


@dataclass
class TopicStats:
    found: int = 0
    new: int = 0
    duplicates: int = 0
    download_errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "found": self.found,
            "new": self.new,
            "duplicates": self.duplicates,
            "download_errors": self.download_errors,
        }


@dataclass
class CollectStats:
    period_start: datetime
    period_end: datetime
    topics: dict[str, TopicStats] = field(default_factory=dict)
    papers_found: int = 0
    papers_new: int = 0
    duplicates: int = 0
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
            "download_errors": self.download_errors,
            "text_extracted": self.text_extracted,
            "topics": {name: stats.as_dict() for name, stats in self.topics.items()},
        }


def _needs_processing(paper: Paper) -> bool:
    """Новые статьи и те, чья обработка в прошлый раз оборвалась до извлечения текста."""
    if paper.status in (PaperStatus.NEW, PaperStatus.DOWNLOADED):
        return True
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
            download_errors=stats.download_errors,
            text_extracted=stats.text_extracted,
        )
        return stats

    def _collect_topic(
        self, topic: Topic, period_start: datetime, period_end: datetime, stats: TopicStats
    ) -> list[int]:
        query = build_search_query(topic.keywords, topic.arxiv_categories, period_start, period_end)
        log.info("collect_topic_started", topic=topic.name, topic_id=topic.id, query=query)
        entries = self._source.search(query, self._max_results)
        stats.found = len(entries)

        paper_ids: list[int] = []
        for entry in entries:
            paper = self._session.scalar(select(Paper).where(Paper.arxiv_id == entry.arxiv_id))
            if paper is None:
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
                stats.new += 1
            else:
                # статья уже известна: по другой теме или из прошлого запуска
                stats.duplicates += 1

            link = self._session.get(PaperTopic, (paper.id, topic.id))
            matched = match_keywords(entry, topic.keywords)
            if link is None:
                self._session.add(
                    PaperTopic(paper_id=paper.id, topic_id=topic.id, matched_keywords=matched)
                )
            else:
                link.matched_keywords = matched
            paper_ids.append(paper.id)

        self._session.commit()
        log.info(
            "collect_topic_done",
            topic=topic.name,
            found=stats.found,
            new=stats.new,
            duplicates=stats.duplicates,
        )
        return paper_ids

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
