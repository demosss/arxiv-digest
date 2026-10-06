from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import cli
from app.core.config import get_settings
from app.models import Topic
from app.services import topic_preview
from tests.factories import FakeSource, make_entry

DATASETS = "(ti:dataset OR ti:benchmark) AND (all:railway OR all:rail)"

YAML = """
topics:
  - name: "ODD"
    description: "Operational Design Domain"
    categories: []
    is_active: true
    queries:
      - 'all:"operational design domain"'
      - 'abs:ODD AND (abs:autonomous OR abs:automated)'
  - name: "Датасеты"
    categories: ["cs.CV", "cs.RO"]
    is_active: false
    queries:
      - '(ti:dataset OR ti:benchmark) AND (all:railway OR all:rail)'
      - autonomous train
"""


def _upload(text: str) -> dict[str, Any]:
    return {"file": ("topics.yaml", text.encode("utf-8"), "application/x-yaml")}


async def _topics(client: AsyncClient) -> dict[str, dict[str, Any]]:
    items = (await client.get("/api/topics", params={"limit": 500})).json()["items"]
    return {
        item["name"]: {
            key: item[key] for key in ("description", "queries", "arxiv_categories", "is_active")
        }
        for item in items
    }


# --- валидация при создании и изменении ---


async def test_queries_are_normalized_on_create(auth_client: AsyncClient) -> None:
    response = await auth_client.post(
        "/api/topics",
        json={"name": "Суда", "queries": ["  autonomous   ship ", '"COLREG"', DATASETS]},
    )

    assert response.status_code == 201
    assert response.json()["queries"] == ['all:"autonomous ship"', 'all:"COLREG"', DATASETS]
    assert response.json()["description"] is None


async def test_invalid_query_is_rejected_with_index_and_position(auth_client: AsyncClient) -> None:
    response = await auth_client.post(
        "/api/topics",
        json={"name": "Датасеты", "queries": ["all:ok", "ti:dataset AND (all:rail", "AND ti:x"]},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    errors = {tuple(item["loc"]): item for item in response.json()["detail"]}
    unclosed = errors[("body", "queries", 1)]
    assert unclosed["type"] == "invalid_query"
    assert unclosed["position"] == 15
    assert "позиция 16: Незакрытая скобка" in unclosed["msg"]
    assert errors[("body", "queries", 2)]["position"] == 0
    assert (await auth_client.get("/api/topics")).json()["total"] == 0


async def test_invalid_query_is_rejected_on_update(auth_client: AsyncClient) -> None:
    created = await auth_client.post("/api/topics", json={"name": "LLM", "queries": ["all:LLM"]})

    response = await auth_client.patch(
        f"/api/topics/{created.json()['id']}", json={"queries": ['ti:"unclosed']}
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "queries", 0]
    assert response.json()["detail"][0]["position"] == 3


async def test_legacy_keywords_are_converted_to_queries(auth_client: AsyncClient) -> None:
    created = await auth_client.post(
        "/api/topics", json={"name": "LLM", "keywords": ["large language model", '"LLM"']}
    )

    assert created.status_code == 201
    assert created.json()["queries"] == ['all:"large language model"', 'all:"LLM"']
    assert "keywords" not in created.json()

    updated = await auth_client.patch(
        f"/api/topics/{created.json()['id']}", json={"keywords": ["diffusion"]}
    )
    assert updated.json()["queries"] == ['all:"diffusion"']


# --- validate-query ---


async def test_validate_query_endpoint(auth_client: AsyncClient, client: AsyncClient) -> None:
    ok = await auth_client.post("/api/topics/validate-query", json={"query": " autonomous  ship"})
    assert ok.status_code == 200
    assert ok.json() == {"normalized": 'all:"autonomous ship"', "errors": [], "warnings": []}

    bad = await auth_client.post("/api/topics/validate-query", json={"query": "ti:a and (ti:b"})
    assert bad.json() == {
        "normalized": "ti:a and (ti:b",
        "errors": [{"position": 9, "message": "Незакрытая скобка"}],
        "warnings": [
            {
                "position": 5,
                "message": "Оператор «and» написан строчными и не сработает: замените на AND",
            }
        ],
    }

    empty = await auth_client.post("/api/topics/validate-query", json={"query": "  "})
    assert empty.json()["errors"] == [{"position": 0, "message": "Пустой запрос"}]

    del client.headers["Authorization"]
    assert (
        await client.post("/api/topics/validate-query", json={"query": "all:a"})
    ).status_code == 401


# --- preview ---


@pytest.fixture
def fake_arxiv(monkeypatch: pytest.MonkeyPatch) -> FakeSource:
    source = FakeSource(
        {
            "autonomous ship": [make_entry(f"2410.000{i:02d}") for i in range(12)],
            "ti:dataset": [make_entry("2410.00001"), make_entry("2410.00100")],
        },
        failing_queries={"all:broken"},
    )
    monkeypatch.setattr(topic_preview, "arxiv_source", lambda: nullcontext(source))
    return source


async def test_preview_runs_each_query_without_saving(
    auth_client: AsyncClient, db: Session, fake_arxiv: FakeSource
) -> None:
    response = await auth_client.post(
        "/api/topics/preview",
        json={
            "queries": ["autonomous ship", "ti:dataset AND all:rail", "all:nothing", "all:broken"],
            "categories": ["cs.RO"],
            "days": 7,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["days"] == 7
    ships, datasets, nothing, broken = body["queries"]
    assert ships["query"] == 'all:"autonomous ship"'
    assert ships["total"] == 12
    assert len(ships["examples"]) == 10
    assert ships["examples"][0] == {
        "arxiv_id": "2410.00000",
        "title": "Paper 2410.00000",
        "published_at": "2024-10-05T12:00:00Z",
        "abs_url": "https://arxiv.org/abs/2410.00000",
    }
    assert datasets["total"] == 2
    assert nothing["total"] == 0 and nothing["examples"] == [] and nothing["error"] is None
    assert broken["total"] == 0 and "ошибку" in broken["error"]
    # 12 + 2 статьи, одна общая
    assert body["unique_total"] == 13
    assert body["unique_is_lower_bound"] is False

    assert len(fake_arxiv.queries) == 4
    assert fake_arxiv.queries[0].startswith('(all:"autonomous ship") AND (cat:cs.RO) AND submitted')
    assert db.scalars(select(Topic)).all() == []


async def test_preview_validates_input(auth_client: AsyncClient, fake_arxiv: FakeSource) -> None:
    too_long = await auth_client.post(
        "/api/topics/preview", json={"queries": ["all:a"], "days": 31}
    )
    assert too_long.status_code == 422

    invalid = await auth_client.post("/api/topics/preview", json={"queries": ["(all:a"]})
    assert invalid.status_code == 422
    assert invalid.json()["detail"][0]["loc"] == ["body", "queries", 0]
    assert fake_arxiv.queries == []


async def test_parallel_preview_is_rejected(
    auth_client: AsyncClient, fake_arxiv: FakeSource
) -> None:
    assert topic_preview._preview_lock.acquire(blocking=False)
    try:
        busy = await auth_client.post("/api/topics/preview", json={"queries": ["all:a"]})
    finally:
        topic_preview._preview_lock.release()

    assert busy.status_code == 429
    assert busy.json()["code"] == "preview_busy"
    assert fake_arxiv.queries == []

    # после освобождения блокировки предпросмотр снова доступен
    assert (
        await auth_client.post("/api/topics/preview", json={"queries": ["all:a"]})
    ).status_code == 200


# --- импорт и экспорт ---


async def test_import_dry_run_changes_nothing(auth_client: AsyncClient) -> None:
    response = await auth_client.post(
        "/api/topics/import", params={"dry_run": True}, files=_upload(YAML)
    )

    assert response.status_code == 200
    assert response.json() == {
        "mode": "merge",
        "dry_run": True,
        "applied": False,
        "created": ["ODD", "Датасеты"],
        "updated": [],
        "unchanged": [],
        "deleted": [],
        "errors": [],
    }
    assert await _topics(auth_client) == {}


async def test_import_merge_updates_by_name_and_keeps_others(auth_client: AsyncClient) -> None:
    await auth_client.post("/api/topics", json={"name": "ODD", "queries": ["all:ODD"]})
    await auth_client.post("/api/topics", json={"name": "Старая", "queries": ["all:old"]})

    response = await auth_client.post("/api/topics/import", files=_upload(YAML))

    assert response.status_code == 200
    report = response.json()
    assert report["applied"] is True
    assert (report["created"], report["updated"], report["deleted"]) == (["Датасеты"], ["ODD"], [])
    topics = await _topics(auth_client)
    assert set(topics) == {"ODD", "Датасеты", "Старая"}
    assert topics["ODD"] == {
        "description": "Operational Design Domain",
        "queries": [
            'all:"operational design domain"',
            "abs:ODD AND (abs:autonomous OR abs:automated)",
        ],
        "arxiv_categories": [],
        "is_active": True,
    }
    assert topics["Датасеты"] == {
        "description": None,
        "queries": [DATASETS, 'all:"autonomous train"'],
        "arxiv_categories": ["cs.CV", "cs.RO"],
        "is_active": False,
    }

    again = await auth_client.post("/api/topics/import", files=_upload(YAML))
    assert again.json()["unchanged"] == ["ODD", "Датасеты"]
    assert again.json()["updated"] == []


async def test_import_replace_deletes_missing_topics(auth_client: AsyncClient) -> None:
    await auth_client.post("/api/topics", json={"name": "Старая", "queries": ["all:old"]})

    preview = await auth_client.post(
        "/api/topics/import", params={"mode": "replace", "dry_run": True}, files=_upload(YAML)
    )
    assert preview.json()["deleted"] == ["Старая"]
    assert set(await _topics(auth_client)) == {"Старая"}

    response = await auth_client.post(
        "/api/topics/import", params={"mode": "replace"}, files=_upload(YAML)
    )

    assert response.json()["deleted"] == ["Старая"]
    assert set(await _topics(auth_client)) == {"ODD", "Датасеты"}


async def test_import_with_invalid_query_is_rolled_back(auth_client: AsyncClient) -> None:
    await auth_client.post("/api/topics", json={"name": "ODD", "queries": ["all:ODD"]})
    broken = YAML + "      - '(ti:dataset OR'\n  - name: ''\n    queries: [all:x]\n"

    response = await auth_client.post(
        "/api/topics/import", params={"mode": "replace"}, files=_upload(broken)
    )

    assert response.status_code == 422
    assert response.json()["code"] == "import_invalid"
    errors = {tuple(item["loc"]): item for item in response.json()["detail"]}
    query_error = errors[("topics", 1, "queries", 2)]
    assert query_error["topic"] == "Датасеты"
    assert query_error["position"] == 0
    assert "Незакрытая скобка" in query_error["msg"]
    assert ("topics", 2, "name") in errors
    # ни одна тема из файла не записана, существующая не тронута
    assert await _topics(auth_client) == {
        "ODD": {
            "description": None,
            "queries": ["all:ODD"],
            "arxiv_categories": [],
            "is_active": True,
        }
    }

    dry = await auth_client.post(
        "/api/topics/import", params={"dry_run": True}, files=_upload(broken)
    )
    assert dry.status_code == 200
    assert dry.json()["applied"] is False
    assert len(dry.json()["errors"]) == 2
    assert dry.json()["created"] == []


@pytest.mark.parametrize(
    "text",
    ["topics: [", "- just\n- a list\n", "topics: {}", "topics:\n  - name: A\n  - name: A\n"],
)
async def test_import_rejects_malformed_files(auth_client: AsyncClient, text: str) -> None:
    response = await auth_client.post("/api/topics/import", files=_upload(text))

    assert response.status_code == 422
    assert response.json()["code"] == "import_invalid"


async def test_export_then_import_gives_the_same_topics(auth_client: AsyncClient) -> None:
    await auth_client.post("/api/topics/import", files=_upload(YAML))
    before = await _topics(auth_client)

    exported = await auth_client.get("/api/topics/export")

    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("application/x-yaml")
    assert "topics.yaml" in exported.headers["content-disposition"]
    assert "Датасеты" in exported.text

    unchanged = await auth_client.post(
        "/api/topics/import",
        params={"mode": "replace", "dry_run": True},
        files=_upload(exported.text),
    )
    assert sorted(unchanged.json()["unchanged"]) == sorted(before)
    assert unchanged.json()["created"] == unchanged.json()["updated"] == []

    for item in (await auth_client.get("/api/topics")).json()["items"]:
        await auth_client.delete(f"/api/topics/{item['id']}")
    assert await _topics(auth_client) == {}

    restored = await auth_client.post("/api/topics/import", files=_upload(exported.text))
    assert restored.json()["applied"] is True
    assert await _topics(auth_client) == before


# --- seed при старте ---


def test_seed_topics_only_when_table_is_empty(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = tmp_path / "seed_topics.yaml"
    monkeypatch.setattr(get_settings(), "seed_topics_path", seed)

    assert cli.seed_topics() == 0  # файла нет

    seed.write_text("topics:\n  - name: A\n    queries: ['(ti:a']\n", encoding="utf-8")
    assert cli.seed_topics() == 0  # ошибка в файле не роняет старт
    assert db.scalars(select(Topic)).all() == []

    seed.write_text(YAML, encoding="utf-8")
    assert cli.seed_topics() == 2
    topics = {topic.name: topic for topic in db.scalars(select(Topic)).all()}
    assert topics["Датасеты"].queries == [DATASETS, 'all:"autonomous train"']
    assert topics["Датасеты"].arxiv_categories == ["cs.CV", "cs.RO"]
    assert topics["Датасеты"].is_active is False

    assert cli.seed_topics() == 0  # таблица уже не пуста
