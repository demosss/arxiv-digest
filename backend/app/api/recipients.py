from typing import Annotated

from fastapi import APIRouter, Query, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import (
    CurrentUser,
    PaginationDep,
    ProfileDep,
    SessionDep,
    get_or_404,
    paginate,
)
from app.core.errors import ConflictError
from app.models import Recipient
from app.schemas.common import Page
from app.schemas.recipients import RecipientCreate, RecipientOut, RecipientUpdate
from app.services.audit import apply_changes, audit

router = APIRouter(prefix="/recipients", tags=["recipients"])

_DUPLICATE = "Получатель с таким email уже есть в этом профиле"


@router.get("", response_model=Page[RecipientOut])
async def list_recipients(
    session: SessionDep,
    profile: ProfileDep,
    pagination: PaginationDep,
    is_active: bool | None = None,
    q: Annotated[str | None, Query(description="Поиск по email и имени")] = None,
) -> Page[RecipientOut]:
    stmt = select(Recipient).where(Recipient.profile_id == profile.id).order_by(Recipient.email)
    if is_active is not None:
        stmt = stmt.where(Recipient.is_active == is_active)
    if q:
        stmt = stmt.where(
            or_(
                Recipient.email.icontains(q, autoescape=True),
                Recipient.name.icontains(q, autoescape=True),
            )
        )
    items, total = await paginate(session, stmt, pagination)
    return Page(
        items=[RecipientOut.model_validate(r) for r in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.post("", response_model=RecipientOut, status_code=status.HTTP_201_CREATED)
async def create_recipient(
    payload: RecipientCreate, session: SessionDep, profile: ProfileDep, user: CurrentUser
) -> Recipient:
    recipient = Recipient(profile_id=profile.id, **payload.model_dump())
    session.add(recipient)
    try:
        await session.commit()
    except IntegrityError as exc:
        raise ConflictError(_DUPLICATE) from exc
    audit("create", "recipient", recipient.id, user.username, **payload.model_dump())
    return recipient


@router.get("/{recipient_id}", response_model=RecipientOut)
async def get_recipient(recipient_id: int, session: SessionDep) -> Recipient:
    return await get_or_404(session, Recipient, recipient_id)


@router.patch("/{recipient_id}", response_model=RecipientOut)
async def update_recipient(
    recipient_id: int, payload: RecipientUpdate, session: SessionDep, user: CurrentUser
) -> Recipient:
    recipient = await get_or_404(session, Recipient, recipient_id)
    data = payload.model_dump(exclude_unset=True)
    # имя можно очистить (null), остальные поля null не принимают
    data = {k: v for k, v in data.items() if v is not None or k == "name"}
    changes = apply_changes(recipient, data)
    try:
        await session.commit()
    except IntegrityError as exc:
        raise ConflictError(_DUPLICATE) from exc
    audit("update", "recipient", recipient.id, user.username, changes=changes)
    return recipient


@router.delete("/{recipient_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_recipient(recipient_id: int, session: SessionDep, user: CurrentUser) -> None:
    recipient = await get_or_404(session, Recipient, recipient_id)
    email = recipient.email
    await session.delete(recipient)
    await session.commit()
    audit("delete", "recipient", recipient_id, user.username, email=email)
