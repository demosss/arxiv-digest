"""Файлы записей БД (PDF статей, дайджесты, переводы): каталоги профилей и удаление."""

from collections.abc import Iterable
from pathlib import Path

from app.core.logging import get_logger

log = get_logger(__name__)


def profile_dir(base: Path, profile_id: int) -> Path:
    """Каталог профиля внутри общего: одноимённые файлы разных профилей не пересекаются."""
    return base / str(profile_id)


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
