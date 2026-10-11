"""Celery-задачи шагов пайплайна. Вся логика — в services, здесь только сборка зависимостей.

Сигнатура шага: (context, run_id=...). `context` — словарь параметров (период, digest_id, ...),
в цепочке full_pipeline он приходит из результата предыдущего шага.
"""

import httpx
from celery import Task
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import DigestContent, RunStatus, TaskRun
from app.services.arxiv_client import ArxivClient
from app.services.collector import Collector
from app.services.digest_builder import build_digest
from app.services.llm_client import LLMClient
from app.services.llm_summarizer import Summarizer
from app.services.mailer import run_send_digest
from app.services.periods import period_from_context, period_to_context
from app.services.relevance import RelevanceJudge
from app.services.summarization import fill_abstracts, summarize_papers
from app.tasks.base import Context, StepResult, execute_step
from app.tasks.celery_app import QUEUE_DEFAULT, QUEUE_LLM, QUEUE_MAIL, celery_app

HOUR = 3600


def _collect(session: Session, run: TaskRun, context: Context) -> StepResult:
    settings = get_settings()
    period_start, period_end = period_from_context(context)
    with httpx.Client() as http:
        source = ArxivClient(
            http,
            request_delay=settings.arxiv_request_delay_seconds,
            user_agent=settings.arxiv_user_agent,
        )
        collector = Collector(
            session,
            source,
            papers_dir=settings.papers_dir,
            max_results_per_topic=settings.arxiv_max_results_per_topic,
        )
        stats = collector.run(period_start, period_end)
    return StepResult(stats=stats.as_dict(), context=period_to_context(period_start, period_end))


def _summarize(session: Session, run: TaskRun, context: Context) -> StepResult:
    settings = get_settings()
    llm = LLMClient.from_settings(settings)
    summarizer = Summarizer(llm, context_chars=settings.llm_context_chars)
    if context.get("fill_abstracts"):
        return StepResult(stats=fill_abstracts(session, summarizer, llm).as_dict())
    stats = summarize_papers(
        session,
        summarizer,
        llm,
        paper_ids=context.get("paper_ids"),
        retry_failed=bool(context.get("retry_failed")),
        judge=RelevanceJudge(llm) if settings.llm_relevance_check else None,
    )
    return StepResult(stats=stats.as_dict())


def _build_digest(session: Session, run: TaskRun, context: Context) -> StepResult:
    settings = get_settings()
    period_start, period_end = period_from_context(context)
    built = build_digest(
        session,
        period_start,
        period_end,
        digests_dir=settings.digests_dir,
        timezone=settings.tz,
        content=DigestContent(context.get("digest_content") or settings.digest_content),
    )
    return StepResult(
        stats=built.stats(),
        context={**period_to_context(period_start, period_end), "digest_id": built.digest.id},
    )


def _send_digest(session: Session, run: TaskRun, context: Context) -> StepResult:
    outcome = run_send_digest(
        session,
        get_settings(),
        digest_id=context.get("digest_id"),
        force=bool(context.get("force")),
    )
    stats = outcome.stats.as_dict() if outcome.stats else {}
    if outcome.skip_reason:
        return StepResult(stats=stats, status=RunStatus.SKIPPED, note=outcome.skip_reason)
    return StepResult(stats=stats, context={"digest_id": outcome.digest_id})


@celery_app.task(
    name="pipeline.collect",
    bind=True,
    queue=QUEUE_DEFAULT,
    acks_late=True,
    soft_time_limit=2 * HOUR,
    time_limit=2 * HOUR + 300,
)
def collect_task(self: Task, context: Context | None = None, *, run_id: int) -> Context:
    return execute_step(self, run_id, context, _collect)


@celery_app.task(
    name="pipeline.summarize",
    bind=True,
    queue=QUEUE_LLM,
    acks_late=True,
    soft_time_limit=8 * HOUR,
    time_limit=8 * HOUR + 300,
)
def summarize_task(self: Task, context: Context | None = None, *, run_id: int) -> Context:
    return execute_step(self, run_id, context, _summarize)


@celery_app.task(
    name="pipeline.build_digest",
    bind=True,
    queue=QUEUE_DEFAULT,
    acks_late=True,
    soft_time_limit=HOUR // 2,
    time_limit=HOUR // 2 + 120,
)
def build_digest_task(self: Task, context: Context | None = None, *, run_id: int) -> Context:
    return execute_step(self, run_id, context, _build_digest)


@celery_app.task(
    name="pipeline.send_digest",
    bind=True,
    queue=QUEUE_MAIL,
    acks_late=True,
    soft_time_limit=HOUR,
    time_limit=HOUR + 120,
)
def send_digest_task(self: Task, context: Context | None = None, *, run_id: int) -> Context:
    return execute_step(self, run_id, context, _send_digest)
