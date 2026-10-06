"""Celery-задачи шагов пайплайна. Вся логика — в services, здесь только сборка зависимостей.

Сигнатура шага: (context, run_id=...). `context` — словарь параметров (период, digest_id, ...),
в цепочке full_pipeline он приходит из результата предыдущего шага.
"""

import httpx
from celery import Task
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import TaskRun
from app.services.arxiv_client import ArxivClient
from app.services.collector import Collector
from app.services.periods import period_from_context, period_to_context
from app.tasks.base import Context, StepResult, execute_step
from app.tasks.celery_app import QUEUE_DEFAULT, celery_app

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
