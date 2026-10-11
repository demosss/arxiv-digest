from fastapi import APIRouter, status
from sqlalchemy import select

from app.api.deps import (
    CurrentUser,
    PaginationDep,
    ProfileDep,
    SessionDep,
    get_or_404,
    paginate,
)
from app.models import Schedule
from app.models.enums import TaskType
from app.schemas.common import Page
from app.schemas.schedules import (
    CronValidateRequest,
    CronValidateResponse,
    ScheduleCreate,
    ScheduleOut,
    ScheduleUpdate,
)
from app.services.audit import apply_changes, audit
from app.services.scheduler import InvalidScheduleError, next_fire_time, next_fire_times

router = APIRouter(prefix="/schedules", tags=["schedules"])

PREVIEW_RUNS = 5
# поля, изменение которых сдвигает время следующего запуска
_TIMING_FIELDS = frozenset({"cron_expression", "timezone", "is_active"})


def _refresh_next_fire(schedule: Schedule) -> None:
    schedule.next_fire_at = (
        next_fire_time(schedule.cron_expression, schedule.timezone) if schedule.is_active else None
    )


@router.post("/validate-cron", response_model=CronValidateResponse)
async def validate_cron_expression(payload: CronValidateRequest) -> CronValidateResponse:
    """Проверяет cron-выражение и возвращает 5 ближайших запусков (для live-превью в форме)."""
    try:
        runs = next_fire_times(payload.cron_expression, payload.timezone, PREVIEW_RUNS)
    except InvalidScheduleError as exc:
        return CronValidateResponse(valid=False, error=str(exc))
    return CronValidateResponse(
        valid=True, cron_expression=" ".join(payload.cron_expression.split()), next_runs=runs
    )


@router.get("", response_model=Page[ScheduleOut])
async def list_schedules(
    session: SessionDep,
    profile: ProfileDep,
    pagination: PaginationDep,
    is_active: bool | None = None,
    task_type: TaskType | None = None,
) -> Page[ScheduleOut]:
    stmt = select(Schedule).where(Schedule.profile_id == profile.id).order_by(Schedule.id)
    if is_active is not None:
        stmt = stmt.where(Schedule.is_active == is_active)
    if task_type is not None:
        stmt = stmt.where(Schedule.task_type == task_type)
    items, total = await paginate(session, stmt, pagination)
    return Page(
        items=[ScheduleOut.model_validate(s) for s in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.post("", response_model=ScheduleOut, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    payload: ScheduleCreate, session: SessionDep, profile: ProfileDep, user: CurrentUser
) -> Schedule:
    schedule = Schedule(profile_id=profile.id, **payload.model_dump())
    _refresh_next_fire(schedule)
    session.add(schedule)
    await session.commit()
    audit("create", "schedule", schedule.id, user.username, **payload.model_dump(mode="json"))
    return schedule


@router.get("/{schedule_id}", response_model=ScheduleOut)
async def get_schedule(schedule_id: int, session: SessionDep) -> Schedule:
    return await get_or_404(session, Schedule, schedule_id)


@router.patch("/{schedule_id}", response_model=ScheduleOut)
async def update_schedule(
    schedule_id: int, payload: ScheduleUpdate, session: SessionDep, user: CurrentUser
) -> Schedule:
    schedule = await get_or_404(session, Schedule, schedule_id)
    changes = apply_changes(schedule, payload.model_dump(exclude_unset=True, exclude_none=True))
    if _TIMING_FIELDS & changes.keys():
        _refresh_next_fire(schedule)
    await session.commit()
    audit(
        "update",
        "schedule",
        schedule.id,
        user.username,
        changes={k: str(v) for k, v in changes.items()},
        next_fire_at=schedule.next_fire_at,
    )
    return schedule


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(schedule_id: int, session: SessionDep, user: CurrentUser) -> None:
    schedule = await get_or_404(session, Schedule, schedule_id)
    name = schedule.name
    await session.delete(schedule)
    await session.commit()
    audit("delete", "schedule", schedule_id, user.username, name=name)
