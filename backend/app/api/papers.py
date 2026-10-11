from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import ColumnElement, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, PaginationDep, ProfileDep, SessionDep, paginate
from app.api.runs import ensure_no_active_runs, launch_manual_run, load_run
from app.api.translations import start_translation
from app.api.translations import to_out as translation_out
from app.core.errors import ConflictError, NotFoundError
from app.models import (
    Paper,
    PaperStatus,
    PaperTopic,
    Recipient,
    TaskRun,
    TaskType,
    Translation,
)
from app.schemas.common import DeletedCount, Page
from app.schemas.papers import (
    PaperDetail,
    PaperOut,
    PaperSendRequest,
    PaperTopicOut,
    SummaryOut,
)
from app.schemas.runs import RunDetail
from app.schemas.translations import TranslateRequest, TranslationOut
from app.services.audit import audit
from app.services.files import remove_files
from app.services.paper_mailer import pdf_file
from app.services.translation.service import docx_file

router = APIRouter(prefix="/papers", tags=["papers"])

_LOAD_OPTIONS = (
    selectinload(Paper.topic_links).selectinload(PaperTopic.topic),
    selectinload(Paper.summary),
)


def _topics(paper: Paper) -> list[PaperTopicOut]:
    return [
        PaperTopicOut(
            id=link.topic_id,
            name=link.topic.name,
            matched_keywords=link.matched_keywords,
            matched_queries=link.matched_queries,
            relevance=link.relevance,
            relevance_reason=link.relevance_reason,
        )
        for link in sorted(paper.topic_links, key=lambda link: link.topic.name)
    ]


def _to_out(paper: Paper) -> PaperOut:
    return PaperOut(
        id=paper.id,
        arxiv_id=paper.arxiv_id,
        version=paper.version,
        title=paper.title,
        authors=paper.authors,
        categories=paper.categories,
        published_at=paper.published_at,
        abs_url=paper.abs_url,
        pdf_url=paper.pdf_url,
        status=paper.status,
        error=paper.error,
        text_chars=paper.text_chars,
        created_at=paper.created_at,
        topics=_topics(paper),
        has_summary=paper.summary is not None,
    )


async def _load_paper(session: AsyncSession, paper_id: int) -> Paper:
    paper = await session.scalar(select(Paper).where(Paper.id == paper_id).options(*_LOAD_OPTIONS))
    if paper is None:
        raise NotFoundError("Статья не найдена")
    return paper


async def _translation_files(session: AsyncSession, paper_ids: Any) -> list[str | None]:
    """Файлы переводов статей: записи удалит каскад, а файлы нужно убрать самим."""
    rows = await session.scalars(
        select(Translation.docx_path).where(Translation.paper_id.in_(paper_ids))
    )
    return list(rows.all())


def _conditions(
    profile_id: int,
    topic_id: int | None,
    paper_status: PaperStatus | None,
    date_from: datetime | None,
    date_to: datetime | None,
    q: str | None,
) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [Paper.profile_id == profile_id]
    if topic_id is not None:
        conditions.append(
            Paper.id.in_(select(PaperTopic.paper_id).where(PaperTopic.topic_id == topic_id))
        )
    if paper_status is not None:
        conditions.append(Paper.status == paper_status)
    if date_from is not None:
        conditions.append(Paper.published_at >= date_from)
    if date_to is not None:
        conditions.append(Paper.published_at <= date_to)
    if q:
        conditions.append(
            Paper.title.icontains(q, autoescape=True) | Paper.arxiv_id.icontains(q, autoescape=True)
        )
    return conditions


@router.get("", response_model=Page[PaperOut])
async def list_papers(
    session: SessionDep,
    profile: ProfileDep,
    pagination: PaginationDep,
    topic_id: int | None = None,
    paper_status: Annotated[PaperStatus | None, Query(alias="status")] = None,
    date_from: Annotated[datetime | None, Query(description="Опубликована не раньше")] = None,
    date_to: Annotated[datetime | None, Query(description="Опубликована не позже")] = None,
    q: Annotated[str | None, Query(description="Поиск по названию и arXiv ID")] = None,
) -> Page[PaperOut]:
    stmt = (
        select(Paper)
        .options(*_LOAD_OPTIONS)
        .where(*_conditions(profile.id, topic_id, paper_status, date_from, date_to, q))
        .order_by(Paper.published_at.desc(), Paper.id.desc())
    )
    items, total = await paginate(session, stmt, pagination)
    return Page(
        items=[_to_out(p) for p in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.delete("", response_model=DeletedCount)
async def clear_papers(
    session: SessionDep,
    profile: ProfileDep,
    user: CurrentUser,
    topic_id: int | None = None,
    paper_status: Annotated[PaperStatus | None, Query(alias="status")] = None,
    date_from: Annotated[datetime | None, Query(description="Опубликована не раньше")] = None,
    date_to: Annotated[datetime | None, Query(description="Опубликована не позже")] = None,
    q: Annotated[str | None, Query(description="Поиск по названию и arXiv ID")] = None,
) -> DeletedCount:
    """Удаляет статьи профиля под фильтрами (без фильтров — все) с резюме, переводами, файлами."""
    await ensure_no_active_runs(session, profile.id)
    conditions = _conditions(profile.id, topic_id, paper_status, date_from, date_to, q)
    docx_paths = await _translation_files(session, select(Paper.id).where(*conditions))
    result = await session.execute(delete(Paper).where(*conditions).returning(Paper.pdf_path))
    pdf_paths = result.scalars().all()
    await session.commit()
    files = await run_in_threadpool(remove_files, [*pdf_paths, *docx_paths])
    audit("clear", "paper", None, user.username, deleted=len(pdf_paths), files_removed=files)
    return DeletedCount(deleted=len(pdf_paths))


@router.get("/{paper_id}", response_model=PaperDetail)
async def get_paper(paper_id: int, session: SessionDep) -> PaperDetail:
    paper = await _load_paper(session, paper_id)
    translations = await session.scalars(
        select(Translation).where(Translation.paper_id == paper_id).order_by(Translation.language)
    )
    return PaperDetail(
        **_to_out(paper).model_dump(),
        abstract=paper.abstract,
        updated_at_arxiv=paper.updated_at_arxiv,
        updated_at=paper.updated_at,
        summary=SummaryOut.model_validate(paper.summary) if paper.summary else None,
        translations=[translation_out(item) for item in translations],
        has_pdf=pdf_file(paper) is not None,
    )


@router.delete("/{paper_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_paper(paper_id: int, session: SessionDep, user: CurrentUser) -> None:
    """Удаляет статью с резюме, переводами и файлами; из состава дайджестов она тоже исчезает."""
    profile_id = await session.scalar(select(Paper.profile_id).where(Paper.id == paper_id))
    if profile_id is None:
        raise NotFoundError("Статья не найдена")
    await ensure_no_active_runs(session, profile_id)
    docx_paths = await _translation_files(session, [paper_id])
    row = (
        await session.execute(
            delete(Paper).where(Paper.id == paper_id).returning(Paper.arxiv_id, Paper.pdf_path)
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError("Статья не найдена")
    await session.commit()
    await run_in_threadpool(remove_files, [row.pdf_path, *docx_paths])
    audit("delete", "paper", paper_id, user.username, arxiv_id=row.arxiv_id)


@router.post(
    "/{paper_id}/resummarize", response_model=RunDetail, status_code=status.HTTP_202_ACCEPTED
)
async def resummarize_paper(paper_id: int, session: SessionDep, user: CurrentUser) -> TaskRun:
    """Ставит в очередь повторную суммаризацию статьи; прежнее резюме будет заменено."""
    paper = await _load_paper(session, paper_id)
    if paper.text_chars is None:
        raise ConflictError(
            "У статьи нет извлечённого текста — сначала нужен успешный сбор", code="no_text"
        )
    run_id = await run_in_threadpool(
        launch_manual_run, paper.profile_id, TaskType.SUMMARIZE, {"paper_ids": [paper.id]}
    )
    audit("resummarize", "paper", paper.id, user.username, run_id=run_id, arxiv_id=paper.arxiv_id)
    return await load_run(session, run_id)


@router.post(
    "/{paper_id}/translate", response_model=TranslationOut, status_code=status.HTTP_202_ACCEPTED
)
async def translate_paper(
    paper_id: int, payload: TranslateRequest, session: SessionDep, user: CurrentUser
) -> TranslationOut:
    """Ставит в очередь перевод статьи в docx; прежний перевод на этот язык будет заменён."""
    translation_id = await run_in_threadpool(start_translation, paper_id, payload.language)
    translation = await session.get(Translation, translation_id, populate_existing=True)
    if translation is None:
        raise NotFoundError("Перевод не найден")
    audit(
        "translate",
        "paper",
        paper_id,
        user.username,
        translation_id=translation.id,
        run_id=translation.run_id,
        language=payload.language,
    )
    return translation_out(translation)


@router.post("/{paper_id}/send", response_model=RunDetail, status_code=status.HTTP_202_ACCEPTED)
async def send_paper(
    paper_id: int, payload: PaperSendRequest, session: SessionDep, user: CurrentUser
) -> TaskRun:
    """Ставит в очередь отправку документов статьи (PDF, переводы docx) выбранным получателям."""
    paper = await _load_paper(session, paper_id)
    if payload.include_pdf and pdf_file(paper) is None:
        raise ConflictError("PDF-файл статьи не сохранён: выполните сбор заново", code="no_pdf")
    if payload.translation_ids:
        rows = await session.scalars(
            select(Translation).where(
                Translation.paper_id == paper_id, Translation.id.in_(payload.translation_ids)
            )
        )
        translations = rows.all()
        if len(translations) != len(payload.translation_ids):
            raise NotFoundError("Перевод не найден у этой статьи")
        if any(docx_file(translation) is None for translation in translations):
            raise ConflictError("Файл перевода ещё не готов", code="no_translation_file")
    found = await session.scalars(
        select(Recipient.id).where(
            Recipient.profile_id == paper.profile_id, Recipient.id.in_(payload.recipient_ids)
        )
    )
    if len(found.all()) != len(payload.recipient_ids):
        raise NotFoundError("Получатель не найден")
    params = {"paper_id": paper.id, **payload.model_dump()}
    run_id = await run_in_threadpool(
        launch_manual_run, paper.profile_id, TaskType.SEND_PAPER, params
    )
    audit("send", "paper", paper.id, user.username, run_id=run_id, **payload.model_dump())
    return await load_run(session, run_id)
