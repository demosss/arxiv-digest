"""Импорт и экспорт тем в YAML (тот же формат у `seed_topics.yaml`).

Функции не привязаны к сессии: разбор и план изменений считаются на объектах, а добавляет,
удаляет и коммитит вызывающий код (async в API, sync в `app.cli`).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml
from pydantic import ValidationError

from app.models import Topic
from app.schemas.topics import INVALID_QUERY_TYPE, ImportIssue, TopicCreate, TopicImportReport

ImportMode = Literal["merge", "replace"]

# имена полей схемы -> ключи YAML
_YAML_KEYS = {"arxiv_categories": "categories"}


@dataclass(frozen=True)
class TopicData:
    name: str
    description: str | None
    queries: list[str]
    categories: list[str]
    is_active: bool


@dataclass
class ImportPlan:
    created: list[TopicData] = field(default_factory=list)
    updated: list[tuple[Topic, TopicData]] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    deleted: list[Topic] = field(default_factory=list)


def export_topics_yaml(topics: Iterable[Topic]) -> str:
    data = {
        "topics": [
            {
                "name": topic.name,
                "description": topic.description,
                "categories": list(topic.arxiv_categories),
                "is_active": topic.is_active,
                "queries": list(topic.queries),
            }
            for topic in topics
        ]
    }
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000)


def _issue(loc: list[str | int], msg: str, topic: str | None = None) -> ImportIssue:
    return ImportIssue(loc=loc, msg=msg, topic=topic)


def _parse_topic(index: int, raw: Any) -> tuple[TopicData | None, list[ImportIssue]]:
    if not isinstance(raw, dict):
        return None, [
            _issue(["topics", index], "Тема должна быть словарём с полями name и queries")
        ]
    name = raw.get("name") if isinstance(raw.get("name"), str) else None
    try:
        parsed = TopicCreate.model_validate(
            {
                "name": raw.get("name"),
                "description": raw.get("description"),
                "queries": raw.get("queries"),
                "arxiv_categories": raw.get("categories") or [],
                "is_active": raw.get("is_active", True),
            }
        )
    except ValidationError as exc:
        issues = []
        for error in exc.errors():
            loc = [
                _YAML_KEYS.get(part, part) if isinstance(part, str) else part
                for part in error["loc"]
            ]
            position = (
                error.get("ctx", {}).get("position")
                if error["type"] == INVALID_QUERY_TYPE
                else None
            )
            issues.append(
                ImportIssue(
                    loc=["topics", index, *loc],
                    msg=error["msg"],
                    type=error["type"],
                    position=position,
                    topic=name,
                )
            )
        return None, issues
    return (
        TopicData(
            name=parsed.name,
            description=parsed.description,
            queries=parsed.queries,
            categories=parsed.arxiv_categories,
            is_active=parsed.is_active,
        ),
        [],
    )


def parse_topics_yaml(text: str) -> tuple[list[TopicData], list[ImportIssue]]:
    """Разбирает и проверяет файл целиком: темы возвращаются, только если ошибок нет вовсе."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [], [_issue(["file"], f"Некорректный YAML: {exc}")]
    raw_topics = document.get("topics") if isinstance(document, dict) else None
    if not isinstance(raw_topics, list):
        return [], [_issue(["file"], "В корне файла ожидается ключ topics со списком тем")]

    topics: list[TopicData] = []
    issues: list[ImportIssue] = []
    names: set[str] = set()
    for index, raw in enumerate(raw_topics):
        topic, topic_issues = _parse_topic(index, raw)
        issues.extend(topic_issues)
        if topic is None:
            continue
        if topic.name in names:
            issues.append(
                _issue(["topics", index, "name"], "Название темы повторяется в файле", topic.name)
            )
            continue
        names.add(topic.name)
        topics.append(topic)
    return ([] if issues else topics), issues


def _differs(topic: Topic, data: TopicData) -> bool:
    return (
        topic.description != data.description
        or list(topic.queries) != data.queries
        or list(topic.arxiv_categories) != data.categories
        or topic.is_active != data.is_active
    )


def plan_import(
    existing: Sequence[Topic], items: Sequence[TopicData], mode: ImportMode
) -> ImportPlan:
    """merge: обновить совпавшие по имени и добавить новые; replace — ещё и удалить остальные."""
    by_name = {topic.name: topic for topic in existing}
    plan = ImportPlan()
    for data in items:
        topic = by_name.pop(data.name, None)
        if topic is None:
            plan.created.append(data)
        elif _differs(topic, data):
            plan.updated.append((topic, data))
        else:
            plan.unchanged.append(data.name)
    if mode == "replace":
        plan.deleted = list(by_name.values())
    return plan


def apply_plan(plan: ImportPlan) -> list[Topic]:
    """Меняет обновляемые темы на месте и возвращает новые; удаление — за вызывающим кодом."""
    for topic, data in plan.updated:
        topic.description = data.description
        topic.queries = list(data.queries)
        topic.arxiv_categories = list(data.categories)
        topic.is_active = data.is_active
    return [
        Topic(
            name=data.name,
            description=data.description,
            queries=list(data.queries),
            arxiv_categories=list(data.categories),
            is_active=data.is_active,
        )
        for data in plan.created
    ]


def build_report(
    plan: ImportPlan, issues: list[ImportIssue], *, mode: ImportMode, dry_run: bool
) -> TopicImportReport:
    return TopicImportReport(
        mode=mode,
        dry_run=dry_run,
        applied=not dry_run and not issues,
        created=[data.name for data in plan.created],
        updated=[topic.name for topic, _ in plan.updated],
        unchanged=plan.unchanged,
        deleted=[topic.name for topic in plan.deleted],
        errors=issues,
    )
