"""Подключение к БД: async-движок (asyncpg) для FastAPI, sync-движок (psycopg) для Celery."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import DateTime, Engine, MetaData, create_engine, func
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.core.config import get_settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    # onupdate на стороне Python: значение известно без повторного SELECT (важно для async)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=func.now()
    )


@lru_cache
def get_async_engine() -> AsyncEngine:
    return create_async_engine(get_settings().async_database_url, pool_pre_ping=True)


@lru_cache
def get_async_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_async_engine(), expire_on_commit=False)


@lru_cache
def get_sync_engine() -> Engine:
    return create_engine(get_settings().sync_database_url, pool_pre_ping=True)


@lru_cache
def get_sync_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(get_sync_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-зависимость: сессия на запрос."""
    async with get_async_sessionmaker()() as session:
        yield session
