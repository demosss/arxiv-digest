from pydantic import BaseModel, EmailStr, Field, field_validator

from app.schemas.common import Timestamps


def _normalize_email(value: str) -> str:
    return value.strip().lower()


def _normalize_name(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(value.split()) or None


class RecipientCreate(BaseModel):
    email: EmailStr
    name: str | None = Field(default=None, max_length=255)
    is_active: bool = True

    check_email = field_validator("email")(_normalize_email)
    check_name = field_validator("name")(_normalize_name)


class RecipientUpdate(BaseModel):
    email: EmailStr | None = None
    name: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None

    @field_validator("email")
    @classmethod
    def check_email(cls, value: str | None) -> str | None:
        return None if value is None else _normalize_email(value)

    check_name = field_validator("name")(_normalize_name)


class RecipientOut(Timestamps):
    id: int
    email: str
    name: str | None
    is_active: bool
