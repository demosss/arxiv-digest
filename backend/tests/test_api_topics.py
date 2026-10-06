from typing import Any

import pytest
from httpx import AsyncClient

TOPIC = {
    "name": "Большие языковые модели",
    "keywords": ["large language model", "LLM"],
    "arxiv_categories": ["cs.CL", "cs.AI"],
}


async def test_topic_crud(auth_client: AsyncClient) -> None:
    created = await auth_client.post("/api/topics", json=TOPIC)
    assert created.status_code == 201
    topic = created.json()
    assert topic["is_active"] is True
    assert topic["keywords"] == TOPIC["keywords"]
    topic_id = topic["id"]

    fetched = await auth_client.get(f"/api/topics/{topic_id}")
    assert fetched.status_code == 200
    assert fetched.json() == topic

    updated = await auth_client.patch(
        f"/api/topics/{topic_id}", json={"keywords": ["diffusion"], "is_active": False}
    )
    assert updated.status_code == 200
    assert updated.json()["keywords"] == ["diffusion"]
    assert updated.json()["is_active"] is False
    assert updated.json()["name"] == TOPIC["name"]

    listing = await auth_client.get("/api/topics")
    assert listing.json()["total"] == 1

    deleted = await auth_client.delete(f"/api/topics/{topic_id}")
    assert deleted.status_code == 204
    assert (await auth_client.get(f"/api/topics/{topic_id}")).status_code == 404
    assert (await auth_client.delete(f"/api/topics/{topic_id}")).status_code == 404


async def test_topic_normalizes_keywords(auth_client: AsyncClient) -> None:
    response = await auth_client.post(
        "/api/topics",
        json={"name": "  RAG  ", "keywords": ['  "retrieval  augmented" ', "RAG", "rag", ""]},
    )

    assert response.status_code == 201
    assert response.json()["name"] == "RAG"
    assert response.json()["keywords"] == ["retrieval augmented", "RAG"]
    assert response.json()["arxiv_categories"] == []


async def test_topic_name_must_be_unique(auth_client: AsyncClient) -> None:
    assert (await auth_client.post("/api/topics", json=TOPIC)).status_code == 201

    duplicate = await auth_client.post("/api/topics", json=TOPIC)

    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "conflict"


@pytest.mark.parametrize(
    "patch",
    [
        {"keywords": []},
        {"keywords": ["  "]},
        {"name": "   "},
        {"arxiv_categories": ["not a category"]},
        {"arxiv_categories": ["CS.CL"]},
    ],
)
async def test_topic_validation(auth_client: AsyncClient, patch: dict[str, Any]) -> None:
    response = await auth_client.post("/api/topics", json={**TOPIC, **patch})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_topic_list_filters_and_pagination(auth_client: AsyncClient) -> None:
    for index in range(5):
        await auth_client.post(
            "/api/topics",
            json={"name": f"Тема {index}", "keywords": ["k"], "is_active": index % 2 == 0},
        )

    page = await auth_client.get("/api/topics", params={"limit": 2, "offset": 2})
    assert page.json()["total"] == 5
    assert [t["name"] for t in page.json()["items"]] == ["Тема 2", "Тема 3"]
    assert page.json()["limit"] == 2
    assert page.json()["offset"] == 2

    active = await auth_client.get("/api/topics", params={"is_active": True})
    assert active.json()["total"] == 3

    search = await auth_client.get("/api/topics", params={"q": "тема 4"})
    assert [t["name"] for t in search.json()["items"]] == ["Тема 4"]

    assert (await auth_client.get("/api/topics", params={"limit": 0})).status_code == 422
