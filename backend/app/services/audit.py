"""Аудит-лог действий в админке: кто что создал, изменил или удалил."""

from typing import Any

from app.core.logging import get_logger

log = get_logger("app.audit")


def audit(action: str, entity: str, entity_id: int | None, user: str, **details: Any) -> None:
    log.info("audit", action=action, entity=entity, entity_id=entity_id, user=user, **details)


def apply_changes(obj: object, data: dict[str, Any]) -> dict[str, Any]:
    """Применяет к объекту только реально изменившиеся поля и возвращает их для аудита."""
    changes: dict[str, Any] = {}
    for field, value in data.items():
        if getattr(obj, field) != value:
            setattr(obj, field, value)
            changes[field] = value
    return changes
