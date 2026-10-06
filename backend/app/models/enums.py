from enum import StrEnum
from typing import Any

from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import MappedColumn, mapped_column


class TaskType(StrEnum):
    FULL_PIPELINE = "full_pipeline"
    COLLECT = "collect"
    SUMMARIZE = "summarize"
    BUILD_DIGEST = "build_digest"
    SEND_DIGEST = "send_digest"


class SmtpSecurity(StrEnum):
    NONE = "none"
    STARTTLS = "starttls"
    SSL = "ssl"


class PaperStatus(StrEnum):
    NEW = "new"
    DOWNLOADED = "downloaded"
    TEXT_EXTRACTED = "text_extracted"
    SUMMARIZED = "summarized"
    FAILED = "failed"


class DigestStatus(StrEnum):
    BUILDING = "building"
    READY = "ready"
    SENT = "sent"
    PARTIALLY_SENT = "partially_sent"
    FAILED = "failed"
    EMPTY = "empty"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class RunTrigger(StrEnum):
    SCHEDULE = "schedule"
    MANUAL = "manual"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


def enum_column(enum_cls: type[StrEnum], **kwargs: Any) -> MappedColumn[Any]:
    """Enum хранится строкой (VARCHAR), а не нативным типом PG: новые значения без ALTER TYPE."""
    return mapped_column(
        SAEnum(
            enum_cls,
            native_enum=False,
            length=32,
            values_callable=lambda cls: [member.value for member in cls],
            validate_strings=True,
        ),
        **kwargs,
    )
