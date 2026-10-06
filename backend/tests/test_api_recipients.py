import pytest
from httpx import AsyncClient


async def test_recipient_crud(auth_client: AsyncClient) -> None:
    created = await auth_client.post(
        "/api/recipients", json={"email": "Reader@Example.com", "name": " Иван  Петров "}
    )
    assert created.status_code == 201
    recipient = created.json()
    assert recipient["email"] == "reader@example.com"
    assert recipient["name"] == "Иван Петров"
    assert recipient["is_active"] is True
    recipient_id = recipient["id"]

    toggled = await auth_client.patch(f"/api/recipients/{recipient_id}", json={"is_active": False})
    assert toggled.status_code == 200
    assert toggled.json()["is_active"] is False
    assert toggled.json()["name"] == "Иван Петров"

    cleared = await auth_client.patch(f"/api/recipients/{recipient_id}", json={"name": None})
    assert cleared.json()["name"] is None

    listing = await auth_client.get("/api/recipients", params={"is_active": False})
    assert listing.json()["total"] == 1
    assert (await auth_client.get("/api/recipients", params={"q": "READER"})).json()["total"] == 1

    assert (await auth_client.delete(f"/api/recipients/{recipient_id}")).status_code == 204
    assert (await auth_client.get(f"/api/recipients/{recipient_id}")).status_code == 404


@pytest.mark.parametrize("email", ["not-an-email", "user@", "@example.com", ""])
async def test_recipient_email_validation(auth_client: AsyncClient, email: str) -> None:
    response = await auth_client.post("/api/recipients", json={"email": email})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_recipient_email_is_unique_case_insensitively(auth_client: AsyncClient) -> None:
    assert (await auth_client.post("/api/recipients", json={"email": "a@example.com"})).is_success
    second = await auth_client.post("/api/recipients", json={"email": "b@example.com"})

    duplicate = await auth_client.post("/api/recipients", json={"email": "A@EXAMPLE.COM"})
    assert duplicate.status_code == 409

    conflict_on_update = await auth_client.patch(
        f"/api/recipients/{second.json()['id']}", json={"email": "a@example.com"}
    )
    assert conflict_on_update.status_code == 409
    assert conflict_on_update.json()["code"] == "conflict"
