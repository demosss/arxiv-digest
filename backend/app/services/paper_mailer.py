"""Задача send_paper: документы статьи (исходный PDF, переводы docx) — выбранным получателям."""

import smtplib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import get_logger
from app.models import Paper, Recipient, Translation
from app.services.mailer import (
    MAX_ATTEMPTS,
    RETRY_DELAYS,
    MailerError,
    SmtpConfig,
    SmtpFactory,
    _close,
    _env,
    _new_message,
    describe_error,
    load_smtp_config,
    open_connection,
)
from app.services.translation.languages import language_label
from app.services.translation.service import docx_file

log = get_logger(__name__)

DOCX_SUBTYPE = "vnd.openxmlformats-officedocument.wordprocessingml.document"


@dataclass(frozen=True, slots=True)
class Attachment:
    filename: str
    data: bytes
    subtype: str
    # как документ назван в тексте письма
    description: str


@dataclass
class PaperSendStats:
    paper_id: int
    arxiv_id: str
    recipients_total: int = 0
    sent: int = 0
    failed: int = 0
    attachments: list[str] = field(default_factory=list)
    attachments_size_bytes: int = 0
    # адрес → причина, по которой письмо не доставлено
    errors: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))


def pdf_file(paper: Paper) -> Path | None:
    if not paper.pdf_path:
        return None
    path = Path(paper.pdf_path)
    return path if path.is_file() else None


def collect_attachments(
    session: Session, paper: Paper, *, include_pdf: bool, translation_ids: Sequence[int]
) -> list[Attachment]:
    """Выбранные документы статьи. Файл мог исчезнуть, пока задача ждала в очереди."""
    attachments: list[Attachment] = []
    if include_pdf:
        path = pdf_file(paper)
        if path is None:
            raise MailerError("PDF-файл статьи не найден: выполните сбор заново")
        attachments.append(Attachment(path.name, path.read_bytes(), "pdf", "исходная статья (PDF)"))
    if translation_ids:
        translations = session.scalars(
            select(Translation)
            .where(Translation.paper_id == paper.id, Translation.id.in_(translation_ids))
            .order_by(Translation.language)
        ).all()
        missing = set(translation_ids) - {translation.id for translation in translations}
        if missing:
            raise MailerError(f"Переводы не найдены: {sorted(missing)}")
        for translation in translations:
            file = docx_file(translation)
            label = language_label(translation.language)
            if file is None:
                raise MailerError(f"Файл перевода ({label}) не найден: выполните перевод заново")
            attachments.append(
                Attachment(file.name, file.read_bytes(), DOCX_SUBTYPE, f"перевод, {label} (docx)")
            )
    if not attachments:
        raise MailerError("Не выбрано ни одного документа для отправки")
    return attachments


def paper_subject(paper: Paper) -> str:
    """Тема письма — название статьи на языке оригинала, как оно записано на arXiv."""
    return " ".join(paper.title.split())


def build_paper_message(
    config: SmtpConfig, to_email: str, paper: Paper, attachments: Sequence[Attachment]
) -> EmailMessage:
    """Письмо HTML + текст со сведениями о статье и выбранными документами во вложении."""
    message = _new_message(config, to_email, paper_subject(paper))
    authors = ", ".join(paper.authors)
    listing = "\n".join(f"- {item.description}: {item.filename}" for item in attachments)
    message.set_content(f"{paper.title}\n{authors}\n{paper.abs_url}\n\nВо вложении:\n{listing}\n")
    message.add_alternative(
        _env.get_template("paper_email.html.j2").render(
            paper=paper, authors=authors, attachments=attachments
        ),
        subtype="html",
    )
    for item in attachments:
        message.add_attachment(
            item.data, maintype="application", subtype=item.subtype, filename=item.filename
        )
    return message


class PaperMailer:
    """Отправка по одному письму на получателя через одно SMTP-соединение, с повторами."""

    def __init__(
        self,
        config: SmtpConfig,
        *,
        smtp_factory: SmtpFactory = open_connection,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._smtp_factory = smtp_factory
        self._sleep = sleep
        self._connection: smtplib.SMTP | None = None
        # заполняется, когда к серверу не удалось подключиться за все попытки
        self._server_down: str | None = None

    def send(
        self, paper: Paper, attachments: Sequence[Attachment], emails: Sequence[str]
    ) -> PaperSendStats:
        stats = PaperSendStats(
            paper_id=paper.id,
            arxiv_id=paper.arxiv_id,
            recipients_total=len(emails),
            attachments=[item.filename for item in attachments],
            attachments_size_bytes=sum(len(item.data) for item in attachments),
        )
        log.info(
            "paper_send_started",
            paper_id=paper.id,
            recipients=len(emails),
            attachments=stats.attachments,
            attachments_size_bytes=stats.attachments_size_bytes,
        )
        try:
            for email in emails:
                if self._server_down is not None:
                    # сервер недоступен: остальных получателей не перебираем с паузами впустую
                    error: str | None = self._server_down
                else:
                    error = self._deliver(
                        email, build_paper_message(self._config, email, paper, attachments)
                    )
                if error is None:
                    stats.sent += 1
                else:
                    stats.failed += 1
                    stats.errors[email] = error
        finally:
            _close(self._connection)
            self._connection = None
        log.info("paper_send_done", **stats.as_dict())
        return stats

    def _deliver(self, email: str, message: EmailMessage) -> str | None:
        """До MAX_ATTEMPTS попыток с паузой. Возвращает причину отказа или None при успехе."""
        connect_failures = 0
        error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                if self._connection is None:
                    try:
                        self._connection = self._smtp_factory(self._config)
                    except (smtplib.SMTPException, OSError):
                        connect_failures += 1
                        raise
                self._connection.send_message(message)
            except (smtplib.SMTPException, OSError) as exc:
                error = describe_error(exc)
                log.warning("mail_failed", recipient=email, attempt=attempt, error=error)
                # после ошибки соединение может быть в непригодном состоянии — открываем заново
                _close(self._connection)
                self._connection = None
                if attempt < MAX_ATTEMPTS:
                    self._sleep(RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)])
                continue
            log.info("mail_sent", recipient=email, attempt=attempt)
            return None
        log.error("mail_gave_up", recipient=email, attempts=MAX_ATTEMPTS)
        if connect_failures == MAX_ATTEMPTS:
            self._server_down = error
            log.error("smtp_unavailable", error=error)
        return error


def run_send_paper(
    session: Session,
    settings: Settings,
    *,
    paper_id: int,
    recipient_ids: Sequence[int],
    include_pdf: bool,
    translation_ids: Sequence[int],
    smtp_factory: SmtpFactory = open_connection,
    sleep: Callable[[float], None] = time.sleep,
) -> PaperSendStats:
    """Отправляет выбранные документы статьи выбранным получателям.

    Если письмо не дошло ни до кого, бросается MailerError со статистикой.
    """
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise MailerError(f"Статья {paper_id} не найдена")
    emails = list(
        session.scalars(
            select(Recipient.email).where(Recipient.id.in_(recipient_ids)).order_by(Recipient.email)
        )
    )
    if not emails:
        raise MailerError("Выбранные получатели не найдены: возможно, они удалены")
    attachments = collect_attachments(
        session, paper, include_pdf=include_pdf, translation_ids=translation_ids
    )
    config = load_smtp_config(session, settings)
    stats = PaperMailer(config, smtp_factory=smtp_factory, sleep=sleep).send(
        paper, attachments, emails
    )
    if stats.sent == 0:
        reason = next(iter(stats.errors.values()), "причина неизвестна")
        raise MailerError(
            f"Не удалось отправить документы ни одному из {stats.recipients_total} получателей: "
            f"{reason}",
            stats=stats.as_dict(),
        )
    return stats
