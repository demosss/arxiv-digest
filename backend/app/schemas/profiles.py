from typing import Annotated

from pydantic import BaseModel, StringConstraints

from app.schemas.common import Timestamps

ProfileName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


class ProfileCreate(BaseModel):
    name: ProfileName


class ProfileUpdate(BaseModel):
    name: ProfileName


class ProfileCounts(BaseModel):
    """Сколько данных в профиле; удалить можно только профиль, где всё по нулям."""

    topics: int = 0
    recipients: int = 0
    schedules: int = 0
    papers: int = 0
    digests: int = 0


class ProfileOut(Timestamps):
    id: int
    name: str
    counts: ProfileCounts
