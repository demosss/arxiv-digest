from fastapi import APIRouter, Depends

from app.api import (
    auth,
    dashboard,
    digests,
    health,
    logs,
    papers,
    recipients,
    runs,
    schedules,
    settings,
    smtp,
    topics,
)
from app.api.deps import get_current_user

api_router = APIRouter(prefix="/api")

# без авторизации доступны только health и login
api_router.include_router(health.router)
api_router.include_router(auth.public_router)

protected = APIRouter(dependencies=[Depends(get_current_user)])
protected.include_router(auth.router)
protected.include_router(topics.router)
protected.include_router(recipients.router)
protected.include_router(schedules.router)
protected.include_router(smtp.router)
protected.include_router(runs.router)
protected.include_router(logs.router)
protected.include_router(papers.router)
protected.include_router(digests.router)
protected.include_router(dashboard.router)
protected.include_router(settings.router)
protected.include_router(health.llm_router)
api_router.include_router(protected)
