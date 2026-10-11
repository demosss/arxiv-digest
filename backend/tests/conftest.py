from collections.abc import AsyncIterator, Iterator

import bcrypt
import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import Base, get_async_engine, get_sync_engine, get_sync_sessionmaker
from app.core.security import create_access_token
from app.models import AdminUser
from app.services.profiles import ensure_default_profile

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "correct-horse-battery"
# профиль по умолчанию: создаётся перед каждым тестом, без заголовка API работает с ним
PROFILE_ID = 1
# минимальная стоимость bcrypt, чтобы тесты не тратили время на хеширование
_ADMIN_HASH = bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()


@pytest.fixture(scope="session")
def _migrated_db() -> Iterator[None]:
    """Чистая схема, созданная миграциями Alembic (заодно проверяет сами миграции)."""
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    command.upgrade(Config("alembic.ini"), "head")
    yield
    engine.dispose()


@pytest.fixture
def db(_migrated_db: None) -> Iterator[Session]:
    """Sync-сессия с профилем по умолчанию; после теста все таблицы очищаются."""
    with get_sync_sessionmaker()() as session:
        assert ensure_default_profile(session).id == PROFILE_ID
        session.commit()
        yield session
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with get_sync_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def admin(db: Session) -> AdminUser:
    user = AdminUser(username=ADMIN_USERNAME, password_hash=_ADMIN_HASH)
    db.add(user)
    db.commit()
    return user


@pytest.fixture
async def client(db: Session) -> AsyncIterator[AsyncClient]:
    """Клиент без авторизации."""
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    await get_async_engine().dispose()


@pytest.fixture
async def auth_client(client: AsyncClient, admin: AdminUser) -> AsyncClient:
    """Клиент с валидным JWT админа."""
    token, _ = create_access_token(admin.username)
    client.headers["Authorization"] = f"Bearer {token}"
    return client
