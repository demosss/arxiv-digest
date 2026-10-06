"""Общие зависимости роутеров: сессия БД, текущий пользователь, пагинация."""

from collections.abc import Sequence
from typing import Annotated, Any

import structlog
from fastapi import Depends, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import Base, get_session
from app.core.errors import NotFoundError, UnauthorizedError
from app.core.security import InvalidTokenError, decode_access_token
from app.models import AdminUser

SessionDep = Annotated[AsyncSession, Depends(get_session)]

_bearer = HTTPBearer(auto_error=False, description="JWT из POST /api/auth/login")


async def get_current_user(
    request: Request,
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AdminUser:
    if credentials is None:
        raise UnauthorizedError()
    try:
        username = decode_access_token(credentials.credentials)
    except InvalidTokenError as exc:
        raise UnauthorizedError("Недействительный или просроченный токен") from exc

    user = await session.scalar(select(AdminUser).where(AdminUser.username == username))
    if user is None or not user.is_active:
        raise UnauthorizedError("Пользователь не найден или отключён")

    # для лога HTTP-запроса (middleware) и для всех записей внутри обработчика
    request.state.username = user.username
    structlog.contextvars.bind_contextvars(user=user.username)
    return user


CurrentUser = Annotated[AdminUser, Depends(get_current_user)]


class Pagination:
    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=500, description="Размер страницы")] = 50,
        offset: Annotated[int, Query(ge=0, description="Смещение")] = 0,
    ) -> None:
        self.limit = limit
        self.offset = offset


PaginationDep = Annotated[Pagination, Depends()]


async def paginate(
    session: AsyncSession, stmt: Select[Any], pagination: Pagination
) -> tuple[Sequence[Any], int]:
    """Возвращает страницу ORM-объектов и общее число строк под фильтрами."""
    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = await session.scalars(stmt.limit(pagination.limit).offset(pagination.offset))
    return rows.unique().all(), total or 0


async def get_or_404[ModelT: Base](
    session: AsyncSession, model: type[ModelT], object_id: int
) -> ModelT:
    obj = await session.get(model, object_id)
    if obj is None:
        raise NotFoundError()
    return obj
