"""Хеширование паролей (bcrypt) и JWT."""

from datetime import timedelta
from typing import Any

import bcrypt
import jwt

from app.core.config import get_settings
from app.core.db import utcnow

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TTL = timedelta(hours=12)
# bcrypt учитывает только первые 72 байта пароля
_BCRYPT_MAX_BYTES = 72


class InvalidTokenError(Exception):
    """Токен отсутствует, повреждён или просрочен."""


def _password_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_password_bytes(password), password_hash.encode("ascii"))
    except ValueError:
        return False


def create_access_token(username: str, ttl: timedelta = ACCESS_TOKEN_TTL) -> tuple[str, int]:
    """Возвращает токен и срок его жизни в секундах."""
    now = utcnow()
    payload: dict[str, Any] = {"sub": username, "iat": now, "exp": now + ttl}
    secret = get_settings().secret_key.get_secret_value()
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM), int(ttl.total_seconds())


def decode_access_token(token: str) -> str:
    """Возвращает username из токена или бросает InvalidTokenError."""
    secret = get_settings().secret_key.get_secret_value()
    try:
        payload = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc
    username = payload.get("sub")
    if not isinstance(username, str) or not username:
        raise InvalidTokenError("token has no subject")
    return username
