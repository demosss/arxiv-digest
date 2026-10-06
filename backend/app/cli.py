"""Служебные команды: `python -m app.cli init` выполняется при старте backend."""

import sys

from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import get_sync_sessionmaker
from app.core.logging import configure_logging, get_logger
from app.core.security import hash_password
from app.models import AdminUser

log = get_logger(__name__)


def ensure_data_dirs() -> None:
    settings = get_settings()
    for path in (settings.papers_dir, settings.digests_dir, settings.logs_dir):
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


def main(argv: list[str]) -> int:
    configure_logging("api")
    command = argv[1] if len(argv) > 1 else ""
    if command == "init":
        ensure_data_dirs()
        ensure_admin()
        return 0
    print("usage: python -m app.cli init", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
