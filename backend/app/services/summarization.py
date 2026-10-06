"""Шаг summarize: резюме для статей с извлечённым текстом."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, undefer

from app.core.logging import get_logger
from app.models import Paper, PaperStatus, Summary
from app.services.llm_client import LLMError, LLMHealth, LLMUnavailableError
from app.services.llm_summarizer import SummaryResult

log = get_logger(__name__)

# после стольких ошибок LLM подряд шаг прерывается: модель, скорее всего, недоступна
MAX_CONSECUTIVE_LLM_ERRORS = 3


class PaperSummarizer(Protocol):
    def summarize(self, title: str, abstract: str, full_text: str) -> SummaryResult: ...


class HealthCheck(Protocol):
    def health(self) -> LLMHealth: ...


@dataclass
class SummarizeStats:
    papers_total: int = 0
    summarized: int = 0
    failed: int = 0
    llm_calls: int = 0
    chunks: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))


def select_papers(
    session: Session, paper_ids: Sequence[int] | None, *, retry_failed: bool = False
) -> list[Paper]:
    """Статьи для суммаризации: переданные явно либо все со статусом text_extracted."""
    stmt = select(Paper).options(undefer(Paper.full_text)).order_by(Paper.id)
    if paper_ids:
        stmt = stmt.where(Paper.id.in_(paper_ids), Paper.full_text.is_not(None))
    else:
        statuses = [PaperStatus.TEXT_EXTRACTED]
        if retry_failed:
            statuses.append(PaperStatus.FAILED)
        stmt = stmt.where(Paper.status.in_(statuses), Paper.full_text.is_not(None))
    return list(session.scalars(stmt).all())


def _mark_failed(session: Session, papers: Sequence[Paper], error: str) -> None:
    for paper in papers:
        paper.status = PaperStatus.FAILED
        paper.error = error
    session.commit()


def _save_summary(session: Session, paper: Paper, result: SummaryResult) -> None:
    summary = session.scalar(select(Summary).where(Summary.paper_id == paper.id))
    if summary is None:
        summary = Summary(paper_id=paper.id)
        session.add(summary)
    summary.text_ru = result.text
    summary.model = result.model
    summary.prompt_tokens = result.prompt_tokens
    summary.completion_tokens = result.completion_tokens
    summary.duration_ms = result.duration_ms
    summary.chunks_count = result.chunks_count
    paper.status = PaperStatus.SUMMARIZED
    paper.error = None
    session.commit()


def summarize_papers(
    session: Session,
    summarizer: PaperSummarizer,
    health: HealthCheck,
    *,
    paper_ids: Sequence[int] | None = None,
    retry_failed: bool = False,
) -> SummarizeStats:
    """Суммаризирует статьи по одной. Ошибка статьи не прерывает остальные;

    если недоступна сама модель, оставшиеся статьи помечаются failed
    и бросается LLMUnavailableError.
    """
    papers = select_papers(session, paper_ids, retry_failed=retry_failed)
    stats = SummarizeStats(papers_total=len(papers))
    if not papers:
        log.info("summarize_nothing_to_do")
        return stats

    status = health.health()
    if not status.ok:
        message = (
            f"LLM недоступна по адресу {status.base_url} (модель {status.model}): {status.error}. "
            "Проверьте, что модель запущена, и значения LLM_BASE_URL / LLM_MODEL"
        )
        log.error("summarize_llm_unavailable", error=status.error, papers=len(papers))
        _mark_failed(session, papers, message)
        stats.failed = len(papers)
        raise LLMUnavailableError(message)
    log.info(
        "summarize_batch_started",
        papers=len(papers),
        model=status.model,
        llm_latency_ms=status.latency_ms,
    )

    consecutive_errors = 0
    for index, paper in enumerate(papers):
        with structlog.contextvars.bound_contextvars(paper_id=paper.id, arxiv_id=paper.arxiv_id):
            try:
                result = summarizer.summarize(paper.title, paper.abstract, paper.full_text or "")
            except LLMError as exc:
                session.rollback()
                _mark_failed(session, [paper], str(exc))
                stats.failed += 1
                log.error("paper_summarize_failed", error=str(exc), error_type=type(exc).__name__)
                consecutive_errors += 1 if isinstance(exc, LLMUnavailableError) else 0
                if consecutive_errors >= MAX_CONSECUTIVE_LLM_ERRORS:
                    rest = papers[index + 1 :]
                    _mark_failed(session, rest, str(exc))
                    stats.failed += len(rest)
                    log.error("summarize_aborted", reason=str(exc), skipped_papers=len(rest))
                    raise
                continue

            consecutive_errors = 0
            _save_summary(session, paper, result)
            stats.summarized += 1
            stats.llm_calls += result.llm_calls
            stats.chunks += result.chunks_count
            stats.prompt_tokens += result.prompt_tokens
            stats.completion_tokens += result.completion_tokens
            stats.llm_duration_ms += result.duration_ms
            log.info(
                "paper_summarized",
                chunks=result.chunks_count,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                duration_ms=result.duration_ms,
            )

    log.info("summarize_batch_done", **stats.as_dict())
    return stats
