"""Периодическая задача планировщика: Celery Beat ставит её раз в минуту."""

from app.core.config import get_settings
from app.core.db import get_sync_sessionmaker
from app.core.logging import get_logger
from app.services import runs
from app.services.scheduler import tick
from app.tasks.celery_app import QUEUE_DEFAULT, celery_app
from app.tasks.launcher import launch_run

log = get_logger(__name__)


@celery_app.task(
    name="scheduler.tick",
    queue=QUEUE_DEFAULT,
    acks_late=True,
    soft_time_limit=50,
    time_limit=55,
)
def scheduler_tick() -> int:
    """Запускает расписания, время которых подошло. Возвращает число срабатываний."""
    with get_sync_sessionmaker()() as session:
        stale = runs.fail_stale_runs(session, get_settings().pipeline_stale_hours)
        session.commit()
        if stale:
            log.warning("stale_runs_failed", run_ids=stale)
        fired = tick(session, launch_run)
    return len(fired)
