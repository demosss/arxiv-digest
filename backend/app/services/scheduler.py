"""Работа с cron-расписаниями: валидация и расчёт времени срабатываний."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

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
