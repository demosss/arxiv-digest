"""Celery-приложение: очереди, общие настройки задач и логирование жизненного цикла."""

import os
from typing import Any

import structlog
from celery import Celery
from celery.signals import setup_logging, task_failure, task_postrun, task_prerun, task_retry
from kombu import Queue

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

QUEUE_DEFAULT = "default"
QUEUE_LLM = "llm"
QUEUE_MAIL = "mail"

log = get_logger("app.celery")

celery_app = Celery("arxiv_digest", broker=get_settings().broker_url)
celery_app.conf.update(
    task_default_queue=QUEUE_DEFAULT,
    task_queues=(Queue(QUEUE_DEFAULT), Queue(QUEUE_LLM), Queue(QUEUE_MAIL)),
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    # результаты задач хранятся в task_runs, отдельный result backend не нужен
    task_ignore_result=True,
    result_backend=None,
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    worker_hijack_root_logger=False,
    worker_redirect_stdouts=False,
    imports=("app.tasks.pipeline",),
)


@setup_logging.connect
def _setup_logging(**_: Any) -> None:
    configure_logging(os.environ.get("SERVICE_NAME", "worker"))


@task_prerun.connect
def _on_task_prerun(task_id: str, task: Any, **_: Any) -> None:
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(celery_task_id=task_id, task_name=task.name)
    log.info("task_started", retries=task.request.retries)


@task_postrun.connect
def _on_task_postrun(state: str | None = None, **_: Any) -> None:
    log.info("task_finished", state=state)
    structlog.contextvars.clear_contextvars()


@task_failure.connect
def _on_task_failure(exception: BaseException | None = None, **_: Any) -> None:
    log.error("task_failed", error=str(exception), exc_info=exception)


@task_retry.connect
def _on_task_retry(reason: Any = None, **_: Any) -> None:
    log.warning("task_retry", reason=str(reason))
