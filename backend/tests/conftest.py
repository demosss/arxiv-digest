from collections.abc import AsyncIterator, Iterator

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import Base, get_async_engine, get_sync_engine, get_sync_sessionmaker


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
    """Sync-сессия; после теста все таблицы очищаются."""
    with get_sync_sessionmaker()() as session:
        yield session
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with get_sync_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def client(db: Session) -> AsyncIterator[AsyncClient]:
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    await get_async_engine().dispose()
