from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

SCHEDULE = {
    "name": "Ежедневный дайджест",
    "task_type": "full_pipeline",
    "cron_expression": "0 9 * * *",
    "timezone": "Europe/Moscow",
}


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


async def test_schedule_crud_and_next_fire_at(auth_client: AsyncClient) -> None:
    created = await auth_client.post("/api/schedules", json=SCHEDULE)
    assert created.status_code == 201
    schedule = created.json()
    assert schedule["last_fired_at"] is None
    next_fire = _parse(schedule["next_fire_at"])
    assert next_fire > datetime.now(UTC)
    # 09:00 по Москве — это 06:00 UTC
    assert (next_fire.astimezone(UTC).hour, next_fire.minute) == (6, 0)
    schedule_id = schedule["id"]

    updated = await auth_client.patch(
        f"/api/schedules/{schedule_id}", json={"cron_expression": "30 12 * * 1", "timezone": "UTC"}
    )
    assert updated.status_code == 200
    moved = _parse(updated.json()["next_fire_at"]).astimezone(UTC)
    assert (moved.hour, moved.minute, moved.weekday()) == (12, 30, 0)

    paused = await auth_client.patch(f"/api/schedules/{schedule_id}", json={"is_active": False})
    assert paused.json()["next_fire_at"] is None

    resumed = await auth_client.patch(f"/api/schedules/{schedule_id}", json={"is_active": True})
    assert resumed.json()["next_fire_at"] is not None

    listing = await auth_client.get("/api/schedules", params={"task_type": "full_pipeline"})
    assert listing.json()["total"] == 1
    assert (await auth_client.get("/api/schedules", params={"task_type": "collect"})).json()[
        "total"
    ] == 0

    assert (await auth_client.delete(f"/api/schedules/{schedule_id}")).status_code == 204
    assert (await auth_client.get(f"/api/schedules/{schedule_id}")).status_code == 404


async def test_renaming_schedule_keeps_next_fire_at(auth_client: AsyncClient) -> None:
    created = (await auth_client.post("/api/schedules", json=SCHEDULE)).json()

    renamed = await auth_client.patch(f"/api/schedules/{created['id']}", json={"name": "Другое"})

    assert renamed.json()["name"] == "Другое"
    assert renamed.json()["next_fire_at"] == created["next_fire_at"]


@pytest.mark.parametrize(
    "patch",
    [
        {"cron_expression": "not a cron"},
        {"cron_expression": "61 * * * *"},
        {"cron_expression": "* * * * * *"},
        {"cron_expression": "0 9 * *"},
        {"timezone": "Mars/Olympus"},
        {"task_type": "unknown"},
        {"name": " "},
    ],
)
async def test_schedule_validation(auth_client: AsyncClient, patch: dict[str, str]) -> None:
    response = await auth_client.post("/api/schedules", json={**SCHEDULE, **patch})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_validate_cron_returns_five_next_runs(auth_client: AsyncClient) -> None:
    response = await auth_client.post(
        "/api/schedules/validate-cron",
        json={"cron_expression": "*/15  8-10 * * 1-5", "timezone": "Europe/Moscow"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["cron_expression"] == "*/15 8-10 * * 1-5"
    runs = [_parse(value) for value in body["next_runs"]]
    assert len(runs) == 5
    assert runs == sorted(runs)
    assert runs[0] > datetime.now(UTC)
    assert all(run.astimezone(UTC).hour in (5, 6, 7) for run in runs)
    assert all(run.weekday() < 5 for run in runs)


@pytest.mark.parametrize(
    "payload",
    [
        {"cron_expression": "every day"},
        {"cron_expression": "0 9 * * *", "timezone": "Nowhere/City"},
    ],
)
async def test_validate_cron_reports_errors(
    auth_client: AsyncClient, payload: dict[str, str]
) -> None:
    response = await auth_client.post("/api/schedules/validate-cron", json=payload)

    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert response.json()["error"]
    assert response.json()["next_runs"] == []
