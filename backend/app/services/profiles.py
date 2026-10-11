"""Профили дайджестов: профиль по умолчанию и подсчёт данных профиля."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models import Digest, Paper, Profile, Recipient, Schedule, Topic

log = get_logger(__name__)

# так называется профиль, созданный миграцией: в нём данные, накопленные до появления профилей
DEFAULT_PROFILE_NAME = "Основной"

# данные, которые нужно удалить из профиля, прежде чем удалять его самого:
# поле счётчика -> (колонка профиля в таблице данных, название для сообщения)
PROFILE_DATA = {
    "topics": (Topic.profile_id, "темы"),
    "recipients": (Recipient.profile_id, "получатели"),
    "schedules": (Schedule.profile_id, "расписания"),
    "papers": (Paper.profile_id, "статьи"),
    "digests": (Digest.profile_id, "дайджесты"),
}


def ensure_default_profile(session: Session) -> Profile:
    """Первый профиль: с ним API работает без заголовка `X-Profile-Id`. Создаётся, если их нет."""
    profile = session.scalar(select(Profile).order_by(Profile.id).limit(1))
    if profile is None:
        profile = Profile(name=DEFAULT_PROFILE_NAME)
        session.add(profile)
        session.flush()
        log.info("profile_created", profile_id=profile.id, name=profile.name)
    return profile
