"""Служебные команды: `python -m app.cli init` выполняется при старте backend."""

import sys

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.db import get_sync_sessionmaker
from app.core.logging import configure_logging, get_logger
from app.core.security import hash_password
from app.models import AdminUser, Topic
from app.services.profiles import ensure_default_profile
from app.services.topics_io import apply_plan, parse_topics_yaml, plan_import

log = get_logger(__name__)


def ensure_data_dirs() -> None:
    settings = get_settings()
    for path in (
        settings.papers_dir,
        settings.digests_dir,
        settings.translations_dir,
        settings.logs_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)


def ensure_admin() -> bool:
    """Создаёт админа из ADMIN_USERNAME / ADMIN_PASSWORD, если его ещё нет."""
    settings = get_settings()
    with get_sync_sessionmaker()() as session:
        exists = session.scalar(
            select(AdminUser.id).where(AdminUser.username == settings.admin_username)
        )
        if exists is not None:
            log.info("admin_exists", username=settings.admin_username)
            return False
        session.add(
            AdminUser(
                username=settings.admin_username,
                password_hash=hash_password(settings.admin_password.get_secret_value()),
            )
        )
        session.commit()
    log.info("admin_created", username=settings.admin_username)
    return True


def ensure_profile() -> None:
    """Сервису нужен хотя бы один профиль: без него нечему принадлежать темам и запускам."""
    with get_sync_sessionmaker()() as session:
        ensure_default_profile(session)
        session.commit()


def seed_topics() -> int:
    """Импортирует темы из seed-файла в первый профиль, если таблица topics пуста."""
    path = get_settings().seed_topics_path
    with get_sync_sessionmaker()() as session:
        if session.scalar(select(func.count()).select_from(Topic)):
            return 0
        if not path.is_file():
            log.info("seed_topics_skipped", path=str(path), reason="file_not_found")
            return 0
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            log.error("seed_topics_failed", path=str(path), error=str(exc))
            return 0
        items, issues = parse_topics_yaml(text)
        if issues:
            # ошибка в seed-файле не должна мешать старту сервиса
            log.error(
                "seed_topics_invalid",
                path=str(path),
                errors=[f"{'.'.join(map(str, i.loc))}: {i.msg}" for i in issues],
            )
            return 0
        profile = ensure_default_profile(session)
        topics = apply_plan(plan_import([], items, "merge"), profile.id)
        session.add_all(topics)
        session.commit()
    log.info("topics_seeded", path=str(path), topics=len(topics))
    return len(topics)


def main(argv: list[str]) -> int:
    configure_logging("api")
    command = argv[1] if len(argv) > 1 else ""
    if command == "init":
        ensure_data_dirs()
        ensure_admin()
        ensure_profile()
        seed_topics()
        return 0
    print("usage: python -m app.cli init", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
