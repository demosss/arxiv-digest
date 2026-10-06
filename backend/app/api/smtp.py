from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, SessionDep
from app.core.config import get_settings
from app.models import SMTP_SETTINGS_ID, SmtpSettings
from app.schemas.smtp import SmtpSettingsOut, SmtpSettingsUpdate
from app.services.audit import apply_changes, audit

router = APIRouter(prefix="/smtp", tags=["smtp"])


async def get_or_create_smtp_settings(session: AsyncSession) -> SmtpSettings:
    settings_row = await session.get(SmtpSettings, SMTP_SETTINGS_ID)
    if settings_row is None:
        settings_row = SmtpSettings(id=SMTP_SETTINGS_ID)
        session.add(settings_row)
        await session.commit()
    return settings_row


def to_out(row: SmtpSettings) -> SmtpSettingsOut:
    return SmtpSettingsOut(
        host=row.host,
        port=row.port,
        username=row.username,
        from_email=row.from_email,
        from_name=row.from_name,
        security=row.security,
        timeout_seconds=row.timeout_seconds,
        created_at=row.created_at,
        updated_at=row.updated_at,
        password_set=get_settings().smtp_password_set,
        is_configured=bool(row.host and row.from_email),
    )


@router.get("", response_model=SmtpSettingsOut)
async def get_smtp_settings(session: SessionDep) -> SmtpSettingsOut:
    return to_out(await get_or_create_smtp_settings(session))


@router.put("", response_model=SmtpSettingsOut)
async def update_smtp_settings(
    payload: SmtpSettingsUpdate, session: SessionDep, user: CurrentUser
) -> SmtpSettingsOut:
    row = await get_or_create_smtp_settings(session)
    changes = apply_changes(row, payload.model_dump())
    await session.commit()
    audit(
        "update",
        "smtp_settings",
        row.id,
        user.username,
        changes={k: str(v) for k, v in changes.items()},
    )
    return to_out(row)
