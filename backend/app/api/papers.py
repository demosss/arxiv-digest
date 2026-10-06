from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, PaginationDep, SessionDep, paginate
from app.api.runs import launch_manual_run, load_run
from app.core.errors import ConflictError, NotFoundError
from app.models import Paper, PaperStatus, PaperTopic, TaskRun, TaskType
from app.schemas.common import Page
from app.schemas.papers import PaperDetail, PaperOut, PaperTopicOut, SummaryOut
from app.schemas.runs import RunDetail
from app.services.audit import audit

router = APIRouter(prefix="/papers", tags=["papers"])

_LOAD_OPTIONS = (
    selectinload(Paper.topic_links).selectinload(PaperTopic.topic),
    selectinload(Paper.summary),
)


def _topics(paper: Paper) -> list[PaperTopicOut]:
    return [
        PaperTopicOut(
            id=link.topic_id, name=link.topic.name, matched_keywords=link.matched_keywords
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


@router.get("", response_model=Page[PaperOut])
async def list_papers(
    session: SessionDep,
    pagination: PaginationDep,
    topic_id: int | None = None,
    paper_status: Annotated[PaperStatus | None, Query(alias="status")] = None,
    date_from: Annotated[datetime | None, Query(description="Опубликована не раньше")] = None,
    date_to: Annotated[datetime | None, Query(description="Опубликована не позже")] = None,
    q: Annotated[str | None, Query(description="Поиск по названию и arXiv ID")] = None,
) -> Page[PaperOut]:
    stmt = (
        select(Paper).options(*_LOAD_OPTIONS).order_by(Paper.published_at.desc(), Paper.id.desc())
    )
    if topic_id is not None:
        stmt = stmt.where(
            Paper.id.in_(select(PaperTopic.paper_id).where(PaperTopic.topic_id == topic_id))
        )
    if paper_status is not None:
        stmt = stmt.where(Paper.status == paper_status)
    if date_from is not None:
        stmt = stmt.where(Paper.published_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(Paper.published_at <= date_to)
    if q:
        stmt = stmt.where(
            Paper.title.icontains(q, autoescape=True) | Paper.arxiv_id.icontains(q, autoescape=True)
        )
    items, total = await paginate(session, stmt, pagination)
    return Page(
        items=[_to_out(p) for p in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get("/{paper_id}", response_model=PaperDetail)
async def get_paper(paper_id: int, session: SessionDep) -> PaperDetail:
    paper = await _load_paper(session, paper_id)
    return PaperDetail(
        **_to_out(paper).model_dump(),
        abstract=paper.abstract,
        updated_at_arxiv=paper.updated_at_arxiv,
        updated_at=paper.updated_at,
        summary=SummaryOut.model_validate(paper.summary) if paper.summary else None,
    )


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
        launch_manual_run, TaskType.SUMMARIZE, {"paper_ids": [paper.id]}
    )
    audit("resummarize", "paper", paper.id, user.username, run_id=run_id, arxiv_id=paper.arxiv_id)
    return await load_run(session, run_id)
