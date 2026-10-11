import pytest
from httpx import AsyncClient

from app.api import health


async def test_health_ok(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "check_rabbitmq", lambda: None)

    response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "db": {"ok": True, "error": None},
        "rabbitmq": {"ok": True, "error": None},
    }
    assert response.headers["X-Request-ID"]


async def test_health_degraded_when_rabbitmq_is_down(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> None:
        raise ConnectionRefusedError("rabbitmq is down")

    monkeypatch.setattr(health, "check_rabbitmq", broken)

    response = await client.get("/api/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["db"]["ok"] is True
    assert body["rabbitmq"] == {"ok": False, "error": "ConnectionRefusedError"}


async def test_request_id_is_propagated(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "check_rabbitmq", lambda: None)

    response = await client.get("/api/health", headers={"X-Request-ID": "req-123"})

    assert response.headers["X-Request-ID"] == "req-123"


async def test_unknown_route_uses_unified_error_format(client: AsyncClient) -> None:
    response = await client.get("/api/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found", "code": "not_found"}


async def test_public_settings(auth_client: AsyncClient, client: AsyncClient) -> None:
    response = await auth_client.get("/api/settings")

    assert response.status_code == 200
    body = response.json()
    assert body["timezone"] == "Europe/Moscow"
    assert body["llm_model"] == "test-model"
    assert body["send_empty_digest"] is False
    assert body["digest_content"] == "full"
    # секретов в ответе нет
    assert not [key for key in body if "password" in key or "key" in key or "secret" in key]

    del auth_client.headers["Authorization"]
    assert (await client.get("/api/settings")).status_code == 401
