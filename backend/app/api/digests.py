from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import PaginationDep, SessionDep, paginate
from app.core.errors import NotFoundError
from app.models import Digest, DigestPaper, DigestStatus, Topic
from app.schemas.common import Page
from app.schemas.digests import DeliveryOut, DigestDetail, DigestOut, DigestPaperOut

router = APIRouter(prefix="/digests", tags=["digests"])


def _pdf_file(digest: Digest) -> Path | None:
    if not digest.pdf_path:
        return None
    path = Path(digest.pdf_path)
    return path if path.is_file() else None


def _to_out(digest: Digest) -> DigestOut:
    pdf = _pdf_file(digest)
    return DigestOut(
        id=digest.id,
        period_start=digest.period_start,
        period_end=digest.period_end,
        papers_count=digest.papers_count,
        status=digest.status,
        error=digest.error,
        has_pdf=pdf is not None,
        filename=pdf.name if pdf else None,
        created_at=digest.created_at,
        updated_at=digest.updated_at,
    )


async def load_digest(session: AsyncSession, digest_id: int) -> Digest:
    digest = await session.scalar(
        select(Digest)
        .where(Digest.id == digest_id)
        .options(
            selectinload(Digest.papers).selectinload(DigestPaper.paper),
            selectinload(Digest.deliveries),
        )
        .execution_options(populate_existing=True)
    )
    if digest is None:
        raise NotFoundError("Дайджест не найден")
    return digest


@router.get("", response_model=Page[DigestOut])
async def list_digests(
    session: SessionDep,
    pagination: PaginationDep,
    digest_status: Annotated[DigestStatus | None, Query(alias="status")] = None,
    date_from: Annotated[datetime | None, Query(description="Период начинается не раньше")] = None,
    date_to: Annotated[datetime | None, Query(description="Период кончается не позже")] = None,
) -> Page[DigestOut]:
    stmt = select(Digest).order_by(Digest.period_end.desc(), Digest.id.desc())
    if digest_status is not None:
        stmt = stmt.where(Digest.status == digest_status)
    if date_from is not None:
        stmt = stmt.where(Digest.period_start >= date_from)
    if date_to is not None:
        stmt = stmt.where(Digest.period_end <= date_to)
    items, total = await paginate(session, stmt, pagination)
    return Page(
        items=[_to_out(d) for d in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get("/{digest_id}", response_model=DigestDetail)
async def get_digest(digest_id: int, session: SessionDep) -> DigestDetail:
    """Дайджест с составом (в порядке вывода в PDF) и статусами доставки."""
    digest = await load_digest(session, digest_id)
    topic_ids = {item.topic_id for item in digest.papers if item.topic_id is not None}
    topic_names: dict[int, str] = {}
    if topic_ids:
        rows = await session.execute(select(Topic.id, Topic.name).where(Topic.id.in_(topic_ids)))
        topic_names = {row.id: row.name for row in rows}
    return DigestDetail(
        **_to_out(digest).model_dump(),
        papers=[
            DigestPaperOut(
                position=item.position,
                paper_id=item.paper_id,
                arxiv_id=item.paper.arxiv_id,
                title=item.paper.title,
                abs_url=item.paper.abs_url,
                topic_id=item.topic_id,
                topic_name=topic_names.get(item.topic_id) if item.topic_id is not None else None,
            )
            for item in digest.papers
        ],
        deliveries=[DeliveryOut.model_validate(d) for d in digest.deliveries],
    )


@router.get(
    "/{digest_id}/pdf",
    response_class=FileResponse,
    responses={200: {"content": {"application/pdf": {}}, "description": "PDF-файл дайджеста"}},
)
async def download_digest_pdf(digest_id: int, session: SessionDep) -> FileResponse:
    digest = await session.get(Digest, digest_id)
    if digest is None:
        raise NotFoundError("Дайджест не найден")
    pdf = _pdf_file(digest)
    if pdf is None:
        raise NotFoundError("PDF-файл дайджеста не найден")
    return FileResponse(pdf, media_type="application/pdf", filename=pdf.name)
