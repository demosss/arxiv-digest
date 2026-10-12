"""Файлы записей БД (PDF статей, дайджесты, переводы): каталоги профилей и удаление."""

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from app.core.logging import get_logger

log = get_logger(__name__)

MAX_PROFILE_SLUG = 50
_UNSAFE_FILENAME_CHARS = re.compile(r'[\s\\/:*?"<>|\x00-\x1f]+')


def profile_dir(base: Path, profile_id: int) -> Path:
    """Каталог профиля внутри общего: одноимённые файлы разных профилей не пересекаются."""
    return base / str(profile_id)


def profile_slug(name: str) -> str:
    """Название профиля для имени файла: пробелы и запрещённые в именах символы — дефисом."""
    slug = _UNSAFE_FILENAME_CHARS.sub("-", name).strip("-.")
    return slug[:MAX_PROFILE_SLUG].rstrip("-.")


def digest_filename(start: datetime, end: datetime, profile_name: str | None = None) -> str:
    """Имя PDF дайджеста по периоду в UTC.

    Без профиля — имя файла на диске (профили разведены каталогами); с профилем — имя, под
    которым файл уходит получателю и скачивается из UI.
    """
    first, last = start.astimezone(UTC).date(), end.astimezone(UTC).date()
    parts = ["digest", first.isoformat()]
    if last != first:
        parts.append(last.isoformat())
    slug = profile_slug(profile_name) if profile_name else ""
    if slug:
        parts.insert(1, slug)
    return "_".join(parts) + ".pdf"


def remove_files(paths: Iterable[str | None]) -> int:
    """Удаляет существующие файлы и возвращает их число; ошибка одного файла не мешает остальным."""
    removed = 0
    for raw in paths:
        if not raw:
            continue
        path = Path(raw)
        try:
            if path.is_file():
                path.unlink()
                removed += 1
        except OSError as exc:
            log.warning("file_remove_failed", path=str(path), error=str(exc))
    return removed
