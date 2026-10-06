from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from app.core.logging import configure_logging
from app.core.security import create_access_token
from app.models import AdminUser
from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME

PROTECTED = [
    ("GET", "/api/auth/me"),
    ("POST", "/api/auth/change-password"),
    ("GET", "/api/topics"),
    ("POST", "/api/topics"),
    ("GET", "/api/recipients"),
    ("GET", "/api/schedules"),
    ("POST", "/api/schedules/validate-cron"),
    ("GET", "/api/smtp"),
    ("PUT", "/api/smtp"),
]


async def test_login_returns_token_valid_for_12_hours(
    client: AsyncClient, admin: AdminUser
) -> None:
    response = await client.post(
        "/api/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 12 * 3600

    me = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["username"] == ADMIN_USERNAME
    assert "password_hash" not in me.json()


@pytest.mark.parametrize(
    ("username", "password"),
    [(ADMIN_USERNAME, "wrong-password"), ("nobody", ADMIN_PASSWORD)],
)
async def test_login_with_bad_credentials(
    client: AsyncClient, admin: AdminUser, username: str, password: str
) -> None:
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )

    assert response.status_code == 401
    assert response.json() == {
        "detail": "Неверное имя пользователя или пароль",
        "code": "unauthorized",
    }


async def test_login_rejects_inactive_user(
    client: AsyncClient, admin: AdminUser, db: Session
) -> None:
    admin.is_active = False
    db.commit()

    response = await client.post(
        "/api/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    )

    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path"), PROTECTED)
async def test_protected_endpoints_require_token(
    client: AsyncClient, method: str, path: str
) -> None:
    response = await client.request(method, path)

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_public_endpoints_do_not_require_token(client: AsyncClient) -> None:
    assert (await client.get("/api/health")).status_code in (200, 503)
    assert (await client.get("/api/openapi.json")).status_code == 200


async def test_invalid_and_expired_tokens_are_rejected(
    client: AsyncClient, admin: AdminUser
) -> None:
    garbage = await client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert garbage.status_code == 401

    expired, _ = create_access_token(ADMIN_USERNAME, ttl=timedelta(seconds=-1))
    response = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_token_of_deleted_user_is_rejected(client: AsyncClient, admin: AdminUser) -> None:
    token, _ = create_access_token("ghost")

    response = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


async def test_change_password(auth_client: AsyncClient) -> None:
    new_password = "new-strong-password"

    response = await auth_client.post(
        "/api/auth/change-password",
        json={"current_password": ADMIN_PASSWORD, "new_password": new_password},
    )
    assert response.status_code == 204

    old = await auth_client.post(
        "/api/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    )
    assert old.status_code == 401
    new = await auth_client.post(
        "/api/auth/login", json={"username": ADMIN_USERNAME, "password": new_password}
    )
    assert new.status_code == 200


async def test_change_password_validations(auth_client: AsyncClient) -> None:
    wrong_current = await auth_client.post(
        "/api/auth/change-password",
        json={"current_password": "nope", "new_password": "new-strong-password"},
    )
    assert wrong_current.status_code == 400
    assert wrong_current.json()["code"] == "wrong_password"

    too_short = await auth_client.post(
        "/api/auth/change-password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "short"},
    )
    assert too_short.status_code == 422
    assert too_short.json()["code"] == "validation_error"


async def test_passwords_never_reach_logs_or_error_bodies(
    client: AsyncClient, admin: AdminUser, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("api")
    secret = "my-very-secret-password"

    failed = await client.post(
        "/api/auth/login", json={"username": ADMIN_USERNAME, "password": secret}
    )
    # невалидное тело: ответ 422 не должен возвращать присланные значения
    invalid = await client.post("/api/auth/login", json={"password": secret})

    assert failed.status_code == 401
    assert invalid.status_code == 422
    assert secret not in invalid.text
    output = capsys.readouterr().out
    assert "login_failed" in output
    assert secret not in output
