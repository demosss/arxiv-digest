from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, MappedColumn, mapped_column

from app.core.db import Base, TimestampMixin


class Profile(TimestampMixin, Base):
    """Профиль дайджестов: свои темы, получатели, расписания, запуски, статьи и дайджесты.

    Настройки SMTP и LLM у всех профилей общие.
    """

    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)


def profile_column(*, ondelete: str = "RESTRICT", index: bool = False) -> MappedColumn[int]:
    """Ссылка на профиль. Данные профиль не отпускают: удалить можно только пустой."""
    return mapped_column(ForeignKey("profiles.id", ondelete=ondelete), index=index)
