"""Работа с cron-расписаниями: валидация и расчёт времени срабатываний."""

from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models import RunTrigger, Schedule, TaskRun
from app.services.periods import run_params

log = get_logger(__name__)

CRON_FIELDS = 5
DEFAULT_TIMEZONE = "Europe/Moscow"


class InvalidScheduleError(ValueError):
    """Некорректное cron-выражение или таймзона."""


def get_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidScheduleError(f"Неизвестная таймзона: {name}") from exc


def validate_cron(expression: str) -> str:
    """Возвращает нормализованное выражение (5 полей через пробел) или бросает ошибку."""
    normalized = " ".join(expression.split())
    if len(normalized.split(" ")) != CRON_FIELDS:
        raise InvalidScheduleError(
            "Cron-выражение должно состоять из 5 полей: минута час день месяц день_недели"
        )
    if not croniter.is_valid(normalized):
        raise InvalidScheduleError(f"Некорректное cron-выражение: {normalized}")
    return normalized


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("naive datetime is not allowed")
    return moment.astimezone(UTC)


def next_fire_times(
    expression: str, timezone: str, count: int = 1, after: datetime | None = None
) -> list[datetime]:
    """Ближайшие `count` срабатываний строго после `after` (по умолчанию — сейчас), в UTC."""
    tz = get_timezone(timezone)
    base = _as_utc(after or datetime.now(UTC)).astimezone(tz)
    iterator = croniter(validate_cron(expression), base)
    return [_as_utc(iterator.get_next(datetime)) for _ in range(count)]


def next_fire_time(expression: str, timezone: str, after: datetime | None = None) -> datetime:
    return next_fire_times(expression, timezone, 1, after)[0]


def last_fire_time(expression: str, timezone: str, now: datetime | None = None) -> datetime:
    """Последнее плановое срабатывание, не позже `now`, в UTC."""
    tz = get_timezone(timezone)
    base = _as_utc(now or datetime.now(UTC)).astimezone(tz)
    expression = validate_cron(expression)
    if croniter.match(expression, base):
        return _as_utc(base.replace(second=0, microsecond=0))
    return _as_utc(croniter(expression, base).get_prev(datetime))


# Функция постановки запуска в очередь: app.tasks.launcher.launch_run
# (session, *, task_type, trigger, params, schedule_id) -> TaskRun
LaunchFn = Callable[..., TaskRun]


def tick(session: Session, launch: LaunchFn, now: datetime | None = None) -> list[TaskRun]:
    """Один проход планировщика: запускает расписания, время которых подошло.

    Каждое расписание обрабатывается в своей транзакции под `FOR UPDATE SKIP LOCKED`:
    параллельные тики не возьмут одну и ту же строку, а отметка о срабатывании
    коммитится вместе с записью запуска. Пропущенные срабатывания не догоняются —
    запускается одно, последнее.
    """
    now = _as_utc(now or datetime.now(UTC))
    fired: list[TaskRun] = []
    while True:
        schedule = session.scalar(
            select(Schedule)
            .where(
                Schedule.is_active.is_(True),
                or_(Schedule.next_fire_at.is_(None), Schedule.next_fire_at <= now),
            )
            .order_by(Schedule.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if schedule is None:
            break

        try:
            due = last_fire_time(schedule.cron_expression, schedule.timezone, now)
            upcoming = next_fire_time(schedule.cron_expression, schedule.timezone, now)
        except InvalidScheduleError as exc:
            log.error("schedule_invalid", schedule_id=schedule.id, error=str(exc))
            schedule.is_active = False
            schedule.next_fire_at = None
            session.commit()
            continue

        if schedule.next_fire_at is None:
            # расписание без рассчитанного времени: считаем заново, не запуская задним числом
            schedule.next_fire_at = upcoming
            session.commit()
            continue

        if schedule.last_fired_at is not None and due <= schedule.last_fired_at:
            log.info(
                "schedule_skipped_duplicate",
                schedule_id=schedule.id,
                due=due.isoformat(),
                last_fired_at=schedule.last_fired_at.isoformat(),
            )
            schedule.next_fire_at = upcoming
            session.commit()
            continue

        if schedule.next_fire_at < due:
            log.warning(
                "schedule_missed_firings",
                schedule_id=schedule.id,
                missed_since=schedule.next_fire_at.isoformat(),
                firing_for=due.isoformat(),
            )
        schedule.last_fired_at = due
        schedule.next_fire_at = upcoming
        log.info(
            "schedule_fired",
            schedule_id=schedule.id,
            name=schedule.name,
            task_type=schedule.task_type.value,
            due=due.isoformat(),
            next_fire_at=upcoming.isoformat(),
        )
        # launch коммитит: отметка о срабатывании и запись запуска сохраняются атомарно
        fired.append(
            launch(
                session,
                task_type=schedule.task_type,
                trigger=RunTrigger.SCHEDULE,
                params=run_params(schedule.task_type),
                schedule_id=schedule.id,
            )
        )
    log.debug("scheduler_tick", fired=len(fired), now=now.isoformat())
    return fired
