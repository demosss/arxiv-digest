import asyncio
from collections.abc import Awaitable

from fastapi import APIRouter, Response, status
from kombu import Connection
from pydantic import BaseModel
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import get_async_sessionmaker
from app.core.logging import get_logger
from app.schemas.papers import LLMHealthOut
from app.services.llm_client import LLMClient

log = get_logger(__name__)
router = APIRouter(tags=["health"])
# требует авторизации, в отличие от /health
llm_router = APIRouter(tags=["health"])

RABBITMQ_CONNECT_TIMEOUT = 3


class ComponentHealth(BaseModel):
    ok: bool
    error: str | None = None


class HealthResponse(BaseModel):
    status: str
    db: ComponentHealth
    rabbitmq: ComponentHealth


async def check_db() -> None:
    async with get_async_sessionmaker()() as session:
        await session.execute(text("SELECT 1"))


def check_rabbitmq() -> None:
    with Connection(
        get_settings().broker_url, connect_timeout=RABBITMQ_CONNECT_TIMEOUT
    ) as connection:
        connection.ensure_connection(max_retries=1, timeout=RABBITMQ_CONNECT_TIMEOUT)


async def _probe(name: str, check: Awaitable[None]) -> ComponentHealth:
    try:
        await check
    except Exception as exc:
        log.warning("health_check_failed", component=name, error=str(exc))
        return ComponentHealth(ok=False, error=type(exc).__name__)
    return ComponentHealth(ok=True)


@router.get("/health", response_model=HealthResponse)
async def health(response: Response) -> HealthResponse:
    """Состояние БД и RabbitMQ. 503, если что-то недоступно."""
    db, rabbitmq = await asyncio.gather(
        _probe("db", check_db()),
        _probe("rabbitmq", asyncio.to_thread(check_rabbitmq)),
    )
    healthy = db.ok and rabbitmq.ok
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(status="ok" if healthy else "degraded", db=db, rabbitmq=rabbitmq)


def check_llm() -> LLMHealthOut:
    result = LLMClient.from_settings(get_settings()).health()
    return LLMHealthOut(
        ok=result.ok,
        base_url=result.base_url,
        model=result.model,
        latency_ms=result.latency_ms,
        model_available=result.model_available,
        models=result.models,
        error=result.error,
    )


@llm_router.get("/llm/health", response_model=LLMHealthOut)
async def llm_health() -> LLMHealthOut:
    """Доступность локальной модели: список моделей сервера или короткий тестовый запрос."""
    return await asyncio.to_thread(check_llm)
