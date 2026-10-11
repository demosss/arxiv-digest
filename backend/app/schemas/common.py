from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


class DeletedCount(BaseModel):
    """Результат массовой очистки."""

    deleted: int


class Timestamps(ORMModel):
    created_at: datetime
    updated_at: datetime


def clean_str_list(values: list[str]) -> list[str]:
    """Убирает лишние пробелы, пустые строки и дубликаты (без учёта регистра), сохраняя порядок."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        value = " ".join(raw.split())
        if value and value.lower() not in seen:
            seen.add(value.lower())
            result.append(value)
    return result
