from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from sqlalchemy import ColumnElement, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, PaginationDep, ProfileDep, SessionDep, paginate
from app.api.runs import ensure_no_active_runs, launch_manual_run, load_run
from app.core.errors import ConflictError, NotFoundError
from app.models import Digest, DigestPaper, DigestStatus, TaskRun, TaskType, Topic
from app.schemas.common import DeletedCount, Page
from app.schemas.digests import (
    DeliveryOut,
    DigestDetail,
    DigestOut,
    DigestPaperOut,
    DigestResendRequest,
)
from app.schemas.runs import RunDetail
from app.services.audit import audit
from app.services.files import remove_files

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
        content=digest.content,
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


def _conditions(
    profile_id: int,
    digest_status: DigestStatus | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [Digest.profile_id == profile_id]
    if digest_status is not None:
        conditions.append(Digest.status == digest_status)
    if date_from is not None:
        conditions.append(Digest.period_start >= date_from)
    if date_to is not None:
        conditions.append(Digest.period_end <= date_to)
    return conditions


@router.get("", response_model=Page[DigestOut])
async def list_digests(
    session: SessionDep,
    profile: ProfileDep,
    pagination: PaginationDep,
    digest_status: Annotated[DigestStatus | None, Query(alias="status")] = None,
    date_from: Annotated[datetime | None, Query(description="Период начинается не раньше")] = None,
    date_to: Annotated[datetime | None, Query(description="Период кончается не позже")] = None,
) -> Page[DigestOut]:
    stmt = (
        select(Digest)
        .where(*_conditions(profile.id, digest_status, date_from, date_to))
        .order_by(Digest.period_end.desc(), Digest.id.desc())
    )
    items, total = await paginate(session, stmt, pagination)
    return Page(
        items=[_to_out(d) for d in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.delete("", response_model=DeletedCount)
async def clear_digests(
    session: SessionDep,
    profile: ProfileDep,
    user: CurrentUser,
    digest_status: Annotated[DigestStatus | None, Query(alias="status")] = None,
    date_from: Annotated[datetime | None, Query(description="Период начинается не раньше")] = None,
    date_to: Annotated[datetime | None, Query(description="Период кончается не позже")] = None,
) -> DeletedCount:
    """Удаляет дайджесты профиля под фильтрами (без фильтров — все) с PDF и историей доставки."""
    await ensure_no_active_runs(session, profile.id)
    result = await session.execute(
        delete(Digest)
        .where(*_conditions(profile.id, digest_status, date_from, date_to))
        .returning(Digest.pdf_path)
    )
    pdf_paths = result.scalars().all()
    await session.commit()
    files = await run_in_threadpool(remove_files, pdf_paths)
    audit("clear", "digest", None, user.username, deleted=len(pdf_paths), files_removed=files)
    return DeletedCount(deleted=len(pdf_paths))


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


@router.delete("/{digest_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_digest(digest_id: int, session: SessionDep, user: CurrentUser) -> None:
    """Удаляет дайджест с PDF-файлом и историей доставки; сами статьи остаются."""
    profile_id = await session.scalar(select(Digest.profile_id).where(Digest.id == digest_id))
    if profile_id is None:
        raise NotFoundError("Дайджест не найден")
    await ensure_no_active_runs(session, profile_id)
    row = (
        await session.execute(
            delete(Digest)
            .where(Digest.id == digest_id)
            .returning(Digest.pdf_path, Digest.period_start, Digest.period_end)
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError("Дайджест не найден")
    await session.commit()
    await run_in_threadpool(remove_files, [row.pdf_path])
    audit(
        "delete",
        "digest",
        digest_id,
        user.username,
        period_start=row.period_start.isoformat(),
        period_end=row.period_end.isoformat(),
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


@router.post("/{digest_id}/resend", response_model=RunDetail, status_code=status.HTTP_202_ACCEPTED)
async def resend_digest(
    digest_id: int,
    session: SessionDep,
    user: CurrentUser,
    payload: DigestResendRequest | None = None,
) -> TaskRun:
    """Ставит в очередь повторную рассылку дайджеста.

    По умолчанию письмо уходит всем активным получателям профиля; с `force=false` — только тем,
    кому дайджест ещё не доставлен.
    """
    digest = await session.get(Digest, digest_id)
    if digest is None:
        raise NotFoundError("Дайджест не найден")
    if _pdf_file(digest) is None:
        raise ConflictError("У дайджеста нет PDF-файла: соберите его заново", code="no_pdf")
    force = payload.force if payload is not None else True
    params = {"digest_id": digest.id, "force": force}
    run_id = await run_in_threadpool(
        launch_manual_run, digest.profile_id, TaskType.SEND_DIGEST, params
    )
    audit("resend", "digest", digest.id, user.username, run_id=run_id, force=force)
    return await load_run(session, run_id)
