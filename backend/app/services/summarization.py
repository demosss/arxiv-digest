"""Шаг summarize: проверка релевантности, аннотация и резюме для статей с извлечённым текстом."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, undefer

from app.core.logging import get_logger
from app.models import Paper, PaperStatus, PaperTopic, Relevance, Summary
from app.services.files import remove_files
from app.services.llm_client import LLMError, LLMHealth, LLMUnavailableError
from app.services.llm_summarizer import AbstractResult, SummaryResult
from app.services.relevance import RelevanceVerdict

log = get_logger(__name__)

# после стольких ошибок LLM подряд шаг прерывается: модель, скорее всего, недоступна
MAX_CONSECUTIVE_LLM_ERRORS = 3


class PaperSummarizer(Protocol):
    def summarize(self, title: str, abstract: str, full_text: str) -> SummaryResult: ...

    def summarize_abstract(self, title: str, abstract: str, full_text: str) -> AbstractResult: ...


class HealthCheck(Protocol):
    def health(self) -> LLMHealth: ...


class RelevanceChecker(Protocol):
    def judge(
        self, *, topic: str, description: str | None, title: str, abstract: str
    ) -> RelevanceVerdict | None: ...


@dataclass
class SummarizeStats:
    papers_total: int = 0
    summarized: int = 0
    failed: int = 0
    irrelevant: int = 0
    relevance_checks: int = 0
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
    stmt = (
        select(Paper)
        .options(
            undefer(Paper.full_text),
            selectinload(Paper.topic_links).selectinload(PaperTopic.topic),
        )
        .order_by(Paper.id)
    )
    if paper_ids:
        stmt = stmt.where(Paper.id.in_(paper_ids), Paper.full_text.is_not(None))
    else:
        statuses = [PaperStatus.TEXT_EXTRACTED]
        if retry_failed:
            statuses.append(PaperStatus.FAILED)
        stmt = stmt.where(Paper.status.in_(statuses), Paper.full_text.is_not(None))
    return list(session.scalars(stmt).all())


def _unavailable_message(status: LLMHealth) -> str:
    return (
        f"LLM недоступна по адресу {status.base_url} (модель {status.model}): {status.error}. "
        "Проверьте, что модель запущена, и значения LLM_BASE_URL / LLM_MODEL"
    )


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
    summary.abstract_ru = result.abstract
    summary.model = result.model
    summary.prompt_tokens = result.prompt_tokens
    summary.completion_tokens = result.completion_tokens
    summary.duration_ms = result.duration_ms
    summary.chunks_count = result.chunks_count
    paper.status = PaperStatus.SUMMARIZED
    paper.error = None
    session.commit()


def _is_relevant(
    session: Session, paper: Paper, judge: RelevanceChecker, stats: SummarizeStats
) -> bool:
    """Оценивает статью для тем, где оценки ещё нет. False — не подошла ни к одной теме.

    Статья без тем или без аннотации не проверяется; ответ модели не по формату
    оценкой не считается, так что отбросить статью может только явное «нет» по всем темам.
    """
    if not paper.topic_links or not paper.abstract.strip():
        return True
    for link in paper.topic_links:
        if link.relevance is not None:
            continue
        verdict = judge.judge(
            topic=link.topic.name,
            description=link.topic.description,
            title=paper.title,
            abstract=paper.abstract,
        )
        stats.relevance_checks += 1
        if verdict is None:
            continue
        link.relevance = verdict.relevance
        link.relevance_reason = verdict.reason
        log.info(
            "paper_relevance",
            topic_id=link.topic_id,
            topic=link.topic.name,
            relevance=verdict.relevance.value,
            reason=verdict.reason,
        )
    session.commit()
    return any(link.relevance != Relevance.IRRELEVANT for link in paper.topic_links)


def _mark_irrelevant(session: Session, paper: Paper) -> None:
    """Нерелевантная статья остаётся в БД, чтобы сбор не скачивал её снова, но без PDF и текста."""
    pdf_path = paper.pdf_path
    paper.status = PaperStatus.IRRELEVANT
    paper.error = None
    paper.pdf_path = None
    paper.full_text = None
    paper.text_chars = None
    session.commit()
    remove_files([pdf_path])


def summarize_papers(
    session: Session,
    summarizer: PaperSummarizer,
    health: HealthCheck,
    *,
    paper_ids: Sequence[int] | None = None,
    retry_failed: bool = False,
    judge: RelevanceChecker | None = None,
) -> SummarizeStats:
    """Суммаризирует статьи по одной. Ошибка статьи не прерывает остальные;

    с `judge` статья сначала проверяется на релевантность темам и, если не подошла ни к одной,
    получает статус irrelevant вместо резюме. Явно выбранные статьи (`paper_ids`)
    не проверяются.

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
        message = _unavailable_message(status)
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

    # явно выбранные статьи пользователь хочет суммаризировать в любом случае
    relevance_judge = None if paper_ids else judge
    consecutive_errors = 0
    for index, paper in enumerate(papers):
        with structlog.contextvars.bound_contextvars(paper_id=paper.id, arxiv_id=paper.arxiv_id):
            try:
                if relevance_judge is not None and not _is_relevant(
                    session, paper, relevance_judge, stats
                ):
                    _mark_irrelevant(session, paper)
                    stats.irrelevant += 1
                    consecutive_errors = 0
                    log.info("paper_irrelevant", topics=len(paper.topic_links))
                    continue
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


def fill_abstracts(
    session: Session, summarizer: PaperSummarizer, health: HealthCheck
) -> SummarizeStats:
    """Дописывает краткие аннотации статьям, у которых резюме создано до их появления.

    Резюме и статус статьи не меняются; ошибка статьи оставляет её без аннотации
    и остальные не прерывает. В статистике `summarized` — число дописанных аннотаций.
    """
    papers = list(
        session.scalars(
            select(Paper)
            .join(Paper.summary)
            .where(Paper.status == PaperStatus.SUMMARIZED, Summary.abstract_ru.is_(None))
            .options(selectinload(Paper.summary))
            .order_by(Paper.id)
        ).all()
    )
    stats = SummarizeStats(papers_total=len(papers))
    if not papers:
        log.info("fill_abstracts_nothing_to_do")
        return stats

    status = health.health()
    if not status.ok:
        log.error("summarize_llm_unavailable", error=status.error, papers=len(papers))
        raise LLMUnavailableError(_unavailable_message(status))
    log.info("fill_abstracts_started", papers=len(papers), model=status.model)

    consecutive_errors = 0
    for paper in papers:
        with structlog.contextvars.bound_contextvars(paper_id=paper.id, arxiv_id=paper.arxiv_id):
            # текст статьи нужен, только если у препринта нет аннотации на arXiv
            full_text = "" if paper.abstract.strip() else paper.full_text or ""
            try:
                result = summarizer.summarize_abstract(paper.title, paper.abstract, full_text)
            except LLMError as exc:
                stats.failed += 1
                log.error("paper_abstract_failed", error=str(exc), error_type=type(exc).__name__)
                consecutive_errors += 1 if isinstance(exc, LLMUnavailableError) else 0
                if consecutive_errors >= MAX_CONSECUTIVE_LLM_ERRORS:
                    log.error("fill_abstracts_aborted", reason=str(exc))
                    raise
                continue

            consecutive_errors = 0
            assert paper.summary is not None
            paper.summary.abstract_ru = result.text
            session.commit()
            stats.summarized += 1
            stats.llm_calls += result.llm_calls
            stats.prompt_tokens += result.prompt_tokens
            stats.completion_tokens += result.completion_tokens
            stats.llm_duration_ms += result.duration_ms
            log.info("paper_abstract_filled", duration_ms=result.duration_ms)

    log.info("fill_abstracts_done", **stats.as_dict())
    return stats
