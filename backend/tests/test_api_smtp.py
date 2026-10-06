import pytest
from httpx import AsyncClient

from app.api import smtp as smtp_api
from app.core.config import Settings, get_settings

SMTP = {
    "host": "smtp.example.com",
    "port": 465,
    "username": "mailer",
    "from_email": "digest@example.com",
    "from_name": "arXiv Digest",
    "security": "ssl",
    "timeout_seconds": 20,
}


async def test_smtp_defaults(auth_client: AsyncClient) -> None:
    response = await auth_client.get("/api/smtp")

    assert response.status_code == 200
    body = response.json()
    assert body["host"] == ""
    assert body["security"] == "starttls"
    assert body["is_configured"] is False
    # SMTP_PASSWORD задан в окружении тестов
    assert body["password_set"] is True


async def test_smtp_update(auth_client: AsyncClient) -> None:
    response = await auth_client.put("/api/smtp", json=SMTP)

    assert response.status_code == 200
    body = response.json()
    assert {key: body[key] for key in SMTP} == SMTP
    assert body["is_configured"] is True
    assert (await auth_client.get("/api/smtp")).json() == body


@pytest.mark.parametrize("field", ["password", "smtp_password"])
async def test_smtp_update_does_not_accept_password(auth_client: AsyncClient, field: str) -> None:
    response = await auth_client.put("/api/smtp", json={**SMTP, field: "hunter2-secret"})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert "hunter2-secret" not in response.text
    # настройки не изменились
    assert (await auth_client.get("/api/smtp")).json()["host"] == ""


async def test_smtp_response_never_contains_password(auth_client: AsyncClient) -> None:
    secret = get_settings().smtp_password
    assert secret is not None

    response = await auth_client.put("/api/smtp", json=SMTP)

    assert "password" not in {key for key in response.json() if key != "password_set"}
    assert secret.get_secret_value() not in response.text


async def test_smtp_reports_missing_password(
    auth_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SMTP_PASSWORD", "")
    monkeypatch.setattr(smtp_api, "get_settings", lambda: Settings())

    response = await auth_client.get("/api/smtp")

    assert response.json()["password_set"] is False


@pytest.mark.parametrize(
    "patch",
    [
        {"port": 0},
        {"port": 70000},
        {"security": "tls13"},
        {"from_email": "not-an-email"},
        {"host": "  "},
        {"timeout_seconds": 0},
    ],
)
async def test_smtp_validation(auth_client: AsyncClient, patch: dict[str, object]) -> None:
    response = await auth_client.put("/api/smtp", json={**SMTP, **patch})

    assert response.status_code == 422
