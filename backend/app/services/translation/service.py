"""Перевод статьи целиком: запись в БД, выбор источника (HTML arXiv или PDF), перевод, docx."""

import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models import Paper, TaskRun, Translation, TranslationStatus
from app.services import runs
from app.services.arxiv_client import ArxivError
from app.services.llm_client import LLMHealth, LLMUnavailableError
from app.services.translation.docx_writer import DocxMeta, build_docx
from app.services.translation.html_source import HtmlParseError, html_url, parse_html
from app.services.translation.images import make_image
from app.services.translation.languages import filename_slug
from app.services.translation.model import Document, Image
from app.services.translation.pdf_source import parse_pdf
from app.services.translation.translator import ChatModel, Translator

log = get_logger(__name__)

ACTIVE_STATUSES = (TranslationStatus.QUEUED, TranslationStatus.RUNNING)
MAX_ERROR_LENGTH = 4000
# перевод без запуска считается брошенным не сразу: запись и запуск создаются в два коммита
ORPHAN_GRACE = timedelta(minutes=5)


class TranslationError(Exception):
    """Перевод невозможен: причина показывается пользователю."""


class PageFetcher(Protocol):
    def fetch(self, url: str, what: str = ...) -> tuple[bytes, str]: ...


class TranslationModel(ChatModel, Protocol):
    def health(self) -> LLMHealth: ...


def translation_filename(arxiv_id: str, language: str) -> str:
    return f"{arxiv_id.replace('/', '_')}_{filename_slug(language)}.docx"


def docx_file(translation: Translation) -> Path | None:
    if not translation.docx_path:
        return None
    path = Path(translation.docx_path)
    return path if path.is_file() else None


def fail_orphaned(session: Session) -> list[int]:
    """Помечает failed переводы, запуск которых отменён, упал или так и не появился.

    Отменённая задача завершается сигналом и сама статус перевода не меняет.
    """
    rows = session.scalars(
        select(Translation)
        .outerjoin(TaskRun, Translation.run_id == TaskRun.id)
        .where(
            Translation.status.in_(ACTIVE_STATUSES),
            or_(
                TaskRun.status.in_(runs.TERMINAL_STATUSES),
                TaskRun.id.is_(None) & (Translation.updated_at < utcnow() - ORPHAN_GRACE),
            ),
        )
    ).all()
    for translation in rows:
        translation.status = TranslationStatus.FAILED
        translation.error = "Запуск перевода отменён или прерван"
    if rows:
        session.flush()
        log.warning("translations_orphaned", translation_ids=[t.id for t in rows])
    return [translation.id for translation in rows]


def prepare_translation(session: Session, paper_id: int, language: str) -> Translation:
    """Создаёт запись перевода или возвращает прежнюю в очередь. Не коммитит."""
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise NotFoundError("Статья не найдена")
    fail_orphaned(session)
    translation = session.scalar(
        select(Translation).where(
            Translation.paper_id == paper_id, Translation.language == language
        )
    )
    if translation is None:
        translation = Translation(paper_id=paper_id, language=language)
        session.add(translation)
    elif translation.status in ACTIVE_STATUSES:
        raise ConflictError(
            "Перевод этой статьи на этот язык уже выполняется", code="translation_active"
        )
    translation.status = TranslationStatus.QUEUED
    translation.error = None
    translation.run_id = None
    session.flush()
    return translation


def load_document(paper: Paper, fetcher: PageFetcher) -> Document:
    """Структура статьи: HTML-версия с arXiv, а если её нет или она пуста — сохранённый PDF."""

    loaded = 0

    def load_image(url: str) -> Image | None:
        nonlocal loaded
        try:
            data, _ = fetcher.fetch(url, "Загрузка рисунка")
        except ArxivError as exc:
            log.warning("translation_image_failed", url=url, error=str(exc))
            return None
        loaded += 1
        # рисунки качаются с паузой между запросами к arXiv: без записей лог молчал бы минутами
        log.info("translation_image_loaded", number=loaded, url=url, size_bytes=len(data))
        return make_image(data)

    url = html_url(paper.arxiv_id, paper.version)
    try:
        content, final_url = fetcher.fetch(url, "Загрузка HTML-версии")
        log.info("translation_html_loaded", url=final_url, size_bytes=len(content))
        document = parse_html(content, base_url=final_url, load_image=load_image)
    except ArxivError as exc:
        log.info("translation_html_unavailable", url=url, reason=str(exc))
    except HtmlParseError as exc:
        log.warning("translation_html_unusable", url=url, reason=str(exc))
    else:
        # метаданные arXiv надёжнее разметки: авторы в HTML идут вперемешку с аффилиациями
        document.authors = list(paper.authors)
        document.title = document.title or paper.title
        if not document.abstract and paper.abstract.strip():
            document.abstract = [paper.abstract.strip()]
        return document

    if not paper.pdf_path or not Path(paper.pdf_path).is_file():
        raise TranslationError(
            "У статьи нет HTML-версии на arXiv, а PDF-файл не сохранён: выполните сбор заново"
        )
    return parse_pdf(
        Path(paper.pdf_path),
        title=paper.title,
        authors=list(paper.authors),
        abstract=paper.abstract,
    )


def run_translation(
    session: Session,
    translation_id: int,
    *,
    run_id: int,
    llm: TranslationModel,
    fetcher: PageFetcher,
    translations_dir: Path,
    chunk_chars: int,
    max_tokens: int,
) -> dict[str, Any]:
    """Переводит статью и сохраняет docx. Ошибка записывается в перевод и пробрасывается."""
    translation = session.get(Translation, translation_id)
    if translation is None:
        raise TranslationError(f"Перевод #{translation_id} не найден: возможно, он удалён")
    paper = translation.paper
    translation.status = TranslationStatus.RUNNING
    translation.error = None
    translation.run_id = run_id
    session.commit()
    started = time.perf_counter()
    log.info(
        "translation_run_started",
        translation_id=translation.id,
        paper_id=paper.id,
        arxiv_id=paper.arxiv_id,
        language=translation.language,
    )
    try:
        status = llm.health()
        if not status.ok:
            raise LLMUnavailableError(
                f"LLM недоступна по адресу {status.base_url} (модель {status.model}): "
                f"{status.error}. Проверьте, что модель запущена, "
                "и значения LLM_BASE_URL / LLM_MODEL"
            )
        document = load_document(paper, fetcher)
        translator = Translator(
            llm, language=translation.language, chunk_chars=chunk_chars, max_tokens=max_tokens
        )
        translate_stats = translator.translate(document)
        path = translations_dir / translation_filename(paper.arxiv_id, translation.language)
        docx_stats = build_docx(
            document,
            language=translation.language,
            meta=DocxMeta(
                arxiv_id=paper.arxiv_id,
                version=paper.version,
                abs_url=paper.abs_url,
                published_at=paper.published_at,
                categories=tuple(paper.categories),
            ),
            path=path,
        )
    except Exception as exc:
        session.rollback()
        translation.status = TranslationStatus.FAILED
        translation.error = (str(exc) or type(exc).__name__)[:MAX_ERROR_LENGTH]
        session.commit()
        raise

    translation.status = TranslationStatus.READY
    translation.source = document.source
    translation.docx_path = str(path)
    translation.model = llm.model
    translation.duration_ms = round((time.perf_counter() - started) * 1000)
    session.commit()
    return {
        "translation_id": translation.id,
        "arxiv_id": paper.arxiv_id,
        "language": translation.language,
        "source": document.source,
        **translate_stats.as_dict(),
        **docx_stats.as_dict(),
    }
