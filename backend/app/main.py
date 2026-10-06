from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import api_router
from app.core.config import get_settings
from app.core.db import get_async_engine
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestLoggingMiddleware

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    log.info("api_started", app_env=settings.app_env, log_level=settings.log_level)
    yield
    await get_async_engine().dispose()
    log.info("api_stopped")


def create_app() -> FastAPI:
    configure_logging("api")
    app = FastAPI(
        title="arXiv Digest",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.add_middleware(RequestLoggingMiddleware)
    register_exception_handlers(app)
    app.include_router(api_router)
    return app


app = create_app()
