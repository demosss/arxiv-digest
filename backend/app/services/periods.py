"""Период выборки статей. Все границы — в UTC, обе включительно."""

from datetime import UTC, datetime, timedelta
from typing import Any


class InvalidPeriodError(ValueError):
    pass


def previous_utc_day(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Предыдущий календарный день в UTC: [D 00:00:00, D 23:59:59]."""
    today = (now or datetime.now(UTC)).astimezone(UTC).date()
    start = datetime.combine(today - timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    return start, start + timedelta(days=1) - timedelta(seconds=1)


def resolve_period(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    """Период из явных границ или, если они не заданы, прошедшие сутки."""
    if start is None and end is None:
        return previous_utc_day()
    if start is None or end is None:
        raise InvalidPeriodError("Нужно указать обе границы периода или ни одной")
    if start.tzinfo is None or end.tzinfo is None:
        raise InvalidPeriodError("Границы периода должны содержать таймзону")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if start > end:
        raise InvalidPeriodError("Начало периода позже его конца")
    return start, end


def period_to_context(start: datetime, end: datetime) -> dict[str, str]:
    return {"period_start": start.isoformat(), "period_end": end.isoformat()}


def period_from_context(context: dict[str, Any]) -> tuple[datetime, datetime]:
    """Период из параметров запуска (ISO-строки); по умолчанию — прошедшие сутки."""
    start, end = context.get("period_start"), context.get("period_end")
    return resolve_period(
        datetime.fromisoformat(start) if start else None,
        datetime.fromisoformat(end) if end else None,
    )
