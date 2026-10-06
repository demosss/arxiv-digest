from typing import Annotated

from fastapi import APIRouter, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUser, PaginationDep, SessionDep, get_or_404, paginate
from app.core.errors import ConflictError
from app.models import Topic
from app.schemas.common import Page
from app.schemas.topics import TopicCreate, TopicOut, TopicUpdate
from app.services.audit import apply_changes, audit

router = APIRouter(prefix="/topics", tags=["topics"])

_DUPLICATE = "Тема с таким названием уже существует"


@router.get("", response_model=Page[TopicOut])
async def list_topics(
    session: SessionDep,
    pagination: PaginationDep,
    is_active: bool | None = None,
    q: Annotated[str | None, Query(description="Поиск по названию")] = None,
) -> Page[TopicOut]:
    stmt = select(Topic).order_by(Topic.name)
    if is_active is not None:
        stmt = stmt.where(Topic.is_active == is_active)
    if q:
        stmt = stmt.where(Topic.name.icontains(q, autoescape=True))
    items, total = await paginate(session, stmt, pagination)
    return Page(
        items=[TopicOut.model_validate(t) for t in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.post("", response_model=TopicOut, status_code=status.HTTP_201_CREATED)
async def create_topic(payload: TopicCreate, session: SessionDep, user: CurrentUser) -> Topic:
    topic = Topic(**payload.model_dump())
    session.add(topic)
    try:
        await session.commit()
    except IntegrityError as exc:
        raise ConflictError(_DUPLICATE) from exc
    audit("create", "topic", topic.id, user.username, **payload.model_dump())
    return topic


@router.get("/{topic_id}", response_model=TopicOut)
async def get_topic(topic_id: int, session: SessionDep) -> Topic:
    return await get_or_404(session, Topic, topic_id)


@router.patch("/{topic_id}", response_model=TopicOut)
async def update_topic(
    topic_id: int, payload: TopicUpdate, session: SessionDep, user: CurrentUser
) -> Topic:
    topic = await get_or_404(session, Topic, topic_id)
    data = payload.model_dump(exclude_unset=True)
    # null допустим только для описания: так оно очищается
    changes = apply_changes(
        topic, {k: v for k, v in data.items() if v is not None or k == "description"}
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        raise ConflictError(_DUPLICATE) from exc
    audit("update", "topic", topic.id, user.username, changes=changes)
    return topic


@router.delete("/{topic_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_topic(topic_id: int, session: SessionDep, user: CurrentUser) -> None:
    topic = await get_or_404(session, Topic, topic_id)
    name = topic.name
    await session.delete(topic)
    await session.commit()
    audit("delete", "topic", topic_id, user.username, name=name)
