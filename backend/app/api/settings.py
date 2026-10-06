from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.config import get_settings

router = APIRouter(prefix="/settings", tags=["settings"])


class PublicSettings(BaseModel):
    """Несекретные параметры сервиса из окружения — только для отображения в админке."""

    timezone: str = Field(description="Таймзона, в которой UI и PDF показывают даты")
    app_env: str
    llm_base_url: str
    llm_model: str
    llm_context_chars: int
    arxiv_max_results_per_topic: int
    send_empty_digest: bool
    pipeline_stale_hours: int


@router.get("", response_model=PublicSettings)
async def get_public_settings() -> PublicSettings:
    settings = get_settings()
    return PublicSettings(
        timezone=settings.tz,
        app_env=settings.app_env,
        llm_base_url=settings.llm_base_url,
        llm_model=settings.llm_model,
        llm_context_chars=settings.llm_context_chars,
        arxiv_max_results_per_topic=settings.arxiv_max_results_per_topic,
        send_empty_digest=settings.send_empty_digest,
        pipeline_stale_hours=settings.pipeline_stale_hours,
    )
