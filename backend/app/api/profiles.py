from collections.abc import Iterable

from fastapi import APIRouter, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, SessionDep
from app.api.runs import ensure_no_active_runs
from app.core.errors import ConflictError, NotFoundError
from app.models import Profile
from app.schemas.profiles import ProfileCounts, ProfileCreate, ProfileOut, ProfileUpdate
from app.services.audit import audit
from app.services.profiles import PROFILE_DATA

router = APIRouter(prefix="/profiles", tags=["profiles"])

_DUPLICATE = "Профиль с таким названием уже существует"


async def _counts(session: AsyncSession, profile_ids: Iterable[int]) -> dict[int, ProfileCounts]:
    ids = list(profile_ids)
    counts = {profile_id: ProfileCounts() for profile_id in ids}
    for field, (column, _) in PROFILE_DATA.items():
        rows = await session.execute(
            select(column, func.count()).where(column.in_(ids)).group_by(column)
        )
        for profile_id, count in rows:
            setattr(counts[profile_id], field, count)
    return counts


def _to_out(profile: Profile, counts: ProfileCounts) -> ProfileOut:
    return ProfileOut(
        id=profile.id,
        name=profile.name,
        counts=counts,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


async def _load(session: AsyncSession, profile_id: int) -> Profile:
    profile = await session.get(Profile, profile_id)
    if profile is None:
        raise NotFoundError("Профиль не найден")
    return profile


@router.get("", response_model=list[ProfileOut])
async def list_profiles(session: SessionDep) -> list[ProfileOut]:
    """Все профили в порядке создания: первый используется, когда профиль в запросе не указан."""
    profiles = (await session.scalars(select(Profile).order_by(Profile.id))).all()
    counts = await _counts(session, [profile.id for profile in profiles])
    return [_to_out(profile, counts[profile.id]) for profile in profiles]


@router.post("", response_model=ProfileOut, status_code=status.HTTP_201_CREATED)
async def create_profile(
    payload: ProfileCreate, session: SessionDep, user: CurrentUser
) -> ProfileOut:
    profile = Profile(name=payload.name)
    session.add(profile)
    try:
        await session.commit()
    except IntegrityError as exc:
        raise ConflictError(_DUPLICATE) from exc
    audit("create", "profile", profile.id, user.username, name=profile.name)
    return _to_out(profile, ProfileCounts())


@router.patch("/{profile_id}", response_model=ProfileOut)
async def update_profile(
    profile_id: int, payload: ProfileUpdate, session: SessionDep, user: CurrentUser
) -> ProfileOut:
    profile = await _load(session, profile_id)
    previous = profile.name
    profile.name = payload.name
    try:
        await session.commit()
    except IntegrityError as exc:
        raise ConflictError(_DUPLICATE) from exc
    audit("update", "profile", profile.id, user.username, name=profile.name, previous=previous)
    counts = await _counts(session, [profile.id])
    return _to_out(profile, counts[profile.id])


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(profile_id: int, session: SessionDep, user: CurrentUser) -> None:
    """Удаляет пустой профиль вместе с историей его запусков.

    Профиль с темами, получателями, расписаниями, статьями или дайджестами не удаляется (409
    `profile_not_empty`): их нужно удалить вручную. Единственный профиль удалить нельзя.
    """
    profile = await _load(session, profile_id)
    total = await session.scalar(select(func.count()).select_from(Profile))
    if total == 1:
        raise ConflictError("Единственный профиль удалить нельзя", code="last_profile")
    counts = (await _counts(session, [profile.id]))[profile.id]
    left = [
        f"{label} — {getattr(counts, field)}"
        for field, (_, label) in PROFILE_DATA.items()
        if getattr(counts, field)
    ]
    if left:
        raise ConflictError(
            f"В профиле остались данные ({', '.join(left)}): сначала удалите их",
            code="profile_not_empty",
        )
    await ensure_no_active_runs(session, profile.id)
    name = profile.name
    await session.delete(profile)
    await session.commit()
    audit("delete", "profile", profile_id, user.username, name=name)
