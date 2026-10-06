from fastapi import APIRouter, Request, status
from sqlalchemy import select

from app.api.deps import CurrentUser, SessionDep
from app.core.errors import BadRequestError, UnauthorizedError
from app.core.logging import get_logger
from app.core.security import create_access_token, hash_password, verify_password
from app.models import AdminUser
from app.schemas.auth import ChangePasswordRequest, LoginRequest, TokenResponse, UserOut
from app.services.audit import audit

log = get_logger(__name__)

public_router = APIRouter(prefix="/auth", tags=["auth"])
router = APIRouter(prefix="/auth", tags=["auth"])

# проверяется при входе несуществующего пользователя, чтобы время ответа не выдавало логины
_DUMMY_HASH = hash_password("dummy-password-for-timing")


@public_router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, request: Request, session: SessionDep) -> TokenResponse:
    user = await session.scalar(select(AdminUser).where(AdminUser.username == payload.username))
    password_ok = verify_password(payload.password, user.password_hash if user else _DUMMY_HASH)
    if user is None or not password_ok or not user.is_active:
        log.warning("login_failed", username=payload.username)
        raise UnauthorizedError("Неверное имя пользователя или пароль")

    request.state.username = user.username
    token, expires_in = create_access_token(user.username)
    log.info("login_success", username=user.username)
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> AdminUser:
    return user


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: ChangePasswordRequest, user: CurrentUser, session: SessionDep
) -> None:
    if not verify_password(payload.current_password, user.password_hash):
        raise BadRequestError("Текущий пароль указан неверно", code="wrong_password")
    if payload.new_password == payload.current_password:
        raise BadRequestError("Новый пароль совпадает с текущим", code="same_password")
    user.password_hash = hash_password(payload.new_password)
    await session.commit()
    audit("change_password", "admin_user", user.id, user.username)
