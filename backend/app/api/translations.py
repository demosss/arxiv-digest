from typing import Annotated

from fastapi import APIRouter, Query, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from sqlalchemy import ColumnElement, delete, select
from sqlalchemy.orm import contains_eager

from app.api.deps import CurrentUser, PaginationDep, ProfileDep, SessionDep, paginate
from app.core.db import get_sync_sessionmaker
from app.core.errors import ConflictError, NotFoundError
from app.models import Paper, RunStatus, RunTrigger, TaskType, Translation, TranslationStatus
from app.schemas.common import DeletedCount, Page
from app.schemas.translations import LanguageOut, TranslationListItem, TranslationOut
from app.services.audit import audit
from app.services.files import remove_files
from app.services.translation.languages import LANGUAGES, language_label, normalize_language
from app.services.translation.service import ACTIVE_STATUSES, docx_file, prepare_translation
from app.tasks import launcher

router = APIRouter(prefix="/translations", tags=["translations"])

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def to_out(translation: Translation) -> TranslationOut:
    file = docx_file(translation)
    return TranslationOut(
        id=translation.id,
        paper_id=translation.paper_id,
        language=translation.language,
        language_label=language_label(translation.language),
        status=translation.status,
        source=translation.source,
        error=translation.error,
        model=translation.model,
        run_id=translation.run_id,
        duration_ms=translation.duration_ms,
        has_file=file is not None,
        filename=file.name if file else None,
        created_at=translation.created_at,
        updated_at=translation.updated_at,
    )


def start_translation(paper_id: int, language: str) -> int:
    """Создаёт (или возвращает в очередь) перевод и ставит задачу. Возвращает id перевода."""
    with get_sync_sessionmaker()() as session:
        translation = prepare_translation(session, paper_id, language)
        params = {"translation_id": translation.id, "paper_id": paper_id, "language": language}
        # launch_run коммитит сессию: запись перевода появляется в БД раньше, чем стартует задача
        run = launcher.launch_run(
            session,
            profile_id=translation.paper.profile_id,
            task_type=TaskType.TRANSLATE,
            trigger=RunTrigger.MANUAL,
            params=params,
        )
        translation.run_id = run.id
        if run.status == RunStatus.FAILED:
            translation.status = TranslationStatus.FAILED
            translation.error = run.error
        session.commit()
        return translation.id


def _conditions(
    profile_id: int,
    translation_status: TranslationStatus | None,
    language: str | None,
    q: str | None,
) -> list[ColumnElement[bool]]:
    # перевод принадлежит профилю своей статьи
    conditions: list[ColumnElement[bool]] = [Paper.profile_id == profile_id]
    if translation_status is not None:
        conditions.append(Translation.status == translation_status)
    if language:
        conditions.append(Translation.language == normalize_language(language))
    if q:
        conditions.append(
            Paper.title.icontains(q, autoescape=True) | Paper.arxiv_id.icontains(q, autoescape=True)
        )
    return conditions


@router.get("", response_model=Page[TranslationListItem])
async def list_translations(
    session: SessionDep,
    profile: ProfileDep,
    pagination: PaginationDep,
    translation_status: Annotated[TranslationStatus | None, Query(alias="status")] = None,
    language: Annotated[str | None, Query(description="Код или название языка")] = None,
    q: Annotated[str | None, Query(description="Поиск по названию статьи и arXiv ID")] = None,
) -> Page[TranslationListItem]:
    stmt = (
        select(Translation)
        .join(Translation.paper)
        .options(contains_eager(Translation.paper))
        .where(*_conditions(profile.id, translation_status, language, q))
        .order_by(Translation.updated_at.desc(), Translation.id.desc())
    )
    items, total = await paginate(session, stmt, pagination)
    return Page(
        items=[
            TranslationListItem(
                **to_out(item).model_dump(), arxiv_id=item.paper.arxiv_id, title=item.paper.title
            )
            for item in items
        ],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get("/languages", response_model=list[LanguageOut])
async def list_languages() -> list[LanguageOut]:
    """Языки для выбора в UI; кроме них можно указать любой язык названием."""
    return [LanguageOut(code=language.code, label=language.label) for language in LANGUAGES]


@router.delete("", response_model=DeletedCount)
async def clear_translations(
    session: SessionDep,
    profile: ProfileDep,
    user: CurrentUser,
    translation_status: Annotated[TranslationStatus | None, Query(alias="status")] = None,
    language: Annotated[str | None, Query(description="Код или название языка")] = None,
    q: Annotated[str | None, Query(description="Поиск по названию статьи и arXiv ID")] = None,
) -> DeletedCount:
    """Удаляет переводы профиля под фильтрами (без фильтров — все) вместе с docx-файлами.

    Переводы, которые ещё выполняются, не затрагиваются.
    """
    ids = (
        select(Translation.id)
        .join(Translation.paper)
        .where(
            Translation.status.not_in(ACTIVE_STATUSES),
            *_conditions(profile.id, translation_status, language, q),
        )
    )
    result = await session.execute(
        delete(Translation).where(Translation.id.in_(ids)).returning(Translation.docx_path)
    )
    paths = result.scalars().all()
    await session.commit()
    files = await run_in_threadpool(remove_files, paths)
    audit("clear", "translation", None, user.username, deleted=len(paths), files_removed=files)
    return DeletedCount(deleted=len(paths))


@router.get(
    "/{translation_id}/docx",
    response_class=FileResponse,
    responses={200: {"content": {DOCX_MEDIA_TYPE: {}}, "description": "Файл перевода"}},
)
async def download_translation(translation_id: int, session: SessionDep) -> FileResponse:
    translation = await session.get(Translation, translation_id)
    if translation is None:
        raise NotFoundError("Перевод не найден")
    file = docx_file(translation)
    if file is None:
        raise NotFoundError("Файл перевода не найден")
    return FileResponse(file, media_type=DOCX_MEDIA_TYPE, filename=file.name)


@router.delete("/{translation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_translation(translation_id: int, session: SessionDep, user: CurrentUser) -> None:
    """Удаляет перевод вместе с docx-файлом; статья остаётся."""
    translation = await session.get(Translation, translation_id)
    if translation is None:
        raise NotFoundError("Перевод не найден")
    if translation.status in ACTIVE_STATUSES:
        raise ConflictError(
            "Перевод ещё выполняется: сначала отмените его запуск", code="translation_active"
        )
    path, paper_id, language = translation.docx_path, translation.paper_id, translation.language
    await session.delete(translation)
    await session.commit()
    await run_in_threadpool(remove_files, [path])
    audit(
        "delete", "translation", translation_id, user.username, paper_id=paper_id, language=language
    )
