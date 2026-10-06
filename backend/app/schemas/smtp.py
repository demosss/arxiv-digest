from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.enums import SmtpSecurity
from app.schemas.common import Timestamps


class SmtpSettingsUpdate(BaseModel):
    """Несекретные параметры SMTP. Пароль задаётся только через SMTP_PASSWORD в .env."""

    # лишние поля (в т.ч. password) отклоняются с 422, а не молча игнорируются
    model_config = ConfigDict(extra="forbid")

    host: str = Field(max_length=255)
    port: int = Field(ge=1, le=65535)
    username: str = Field(default="", max_length=255)
    from_email: EmailStr
    from_name: str = Field(default="", max_length=255)
    security: SmtpSecurity
    timeout_seconds: int = Field(default=30, ge=1, le=300)

    @field_validator("host", "username", "from_name")
    @classmethod
    def check_strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("host")
    @classmethod
    def check_host_required(cls, value: str) -> str:
        if not value:
            raise ValueError("Укажите SMTP-сервер")
        return value


class SmtpSettingsOut(Timestamps):
    host: str
    port: int
    username: str
    from_email: str
    from_name: str
    security: SmtpSecurity
    timeout_seconds: int
    password_set: bool = Field(description="Задан ли SMTP_PASSWORD в окружении")
    is_configured: bool = Field(description="Заполнены ли host и from_email")
