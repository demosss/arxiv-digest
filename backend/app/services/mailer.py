"""Шаг send_digest: рассылка PDF-дайджеста по SMTP, отдельное письмо каждому получателю."""

import smtplib
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings
from app.core.db import utcnow
from app.core.logging import get_logger
from app.models import (
    SMTP_SETTINGS_ID,
    DeliveryStatus,
    Digest,
    DigestDelivery,
    DigestPaper,
    DigestStatus,
    Recipient,
    SmtpSecurity,
    SmtpSettings,
)

log = get_logger(__name__)

MAX_ATTEMPTS = 3
# паузы между попытками отправки одному получателю, секунды
RETRY_DELAYS = (5.0, 15.0)
DATE_FORMAT = "%d.%m.%Y"
TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"

_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


class MailerError(Exception):
    """Письма отправить не удалось. `stats` попадает в статистику упавшего запуска."""

    def __init__(self, message: str, stats: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.stats = stats


class MailerConfigError(MailerError):
    """SMTP не настроен или настроен не до конца."""


@dataclass(frozen=True, slots=True)
class SmtpConfig:
    host: str
    port: int
    username: str
    password: str
    from_email: str
    from_name: str
    security: SmtpSecurity
    timeout: int


@dataclass(frozen=True, slots=True)
class PaperLine:
    title: str
    abs_url: str


@dataclass
class SendStats:
    digest_id: int
    recipients_total: int = 0
    sent: int = 0
    failed: int = 0
    skipped_already_sent: int = 0
    digest_status: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))


SmtpFactory = Callable[[SmtpConfig], smtplib.SMTP]


def load_smtp_config(session: Session, settings: Settings) -> SmtpConfig:
    """Несекретные параметры — из БД, пароль — только из SMTP_PASSWORD."""
    row = session.get(SmtpSettings, SMTP_SETTINGS_ID)
    if row is None or not row.host or not row.from_email:
        raise MailerConfigError(
            "SMTP не настроен: укажите сервер и адрес отправителя в разделе «Настройки SMTP»"
        )
    password = settings.smtp_password.get_secret_value() if settings.smtp_password else ""
    if row.username and not password:
        raise MailerConfigError(
            "Для SMTP задан логин, но пароль не задан: добавьте SMTP_PASSWORD в .env"
        )
    return SmtpConfig(
        host=row.host,
        port=row.port,
        username=row.username,
        password=password,
        from_email=row.from_email,
        from_name=row.from_name,
        security=row.security,
        timeout=row.timeout_seconds,
    )


def open_connection(config: SmtpConfig) -> smtplib.SMTP:
    """Подключается к SMTP-серверу в режиме none / starttls / ssl и авторизуется."""
    log.info(
        "smtp_connect",
        host=config.host,
        port=config.port,
        security=config.security.value,
        username=config.username or None,
    )
    started = time.perf_counter()
    connection: smtplib.SMTP
    if config.security == SmtpSecurity.SSL:
        connection = smtplib.SMTP_SSL(
            config.host, config.port, timeout=config.timeout, context=ssl.create_default_context()
        )
    else:
        connection = smtplib.SMTP(config.host, config.port, timeout=config.timeout)
    try:
        connection.ehlo()
        if config.security == SmtpSecurity.STARTTLS:
            connection.starttls(context=ssl.create_default_context())
            connection.ehlo()
        if config.username:
            connection.login(config.username, config.password)
    except Exception:
        connection.close()
        raise
    log.info("smtp_connected", duration_ms=round((time.perf_counter() - started) * 1000))
    return connection


def _close(connection: smtplib.SMTP | None) -> None:
    if connection is None:
        return
    try:
        connection.quit()
    except (smtplib.SMTPException, OSError):
        connection.close()


def digest_subject(digest: Digest) -> str:
    start = digest.period_start.astimezone(UTC).strftime(DATE_FORMAT)
    end = digest.period_end.astimezone(UTC).strftime(DATE_FORMAT)
    period = start if start == end else f"{start} — {end}"
    return f"Дайджест arXiv за {period}"


def _new_message(config: SmtpConfig, to_email: str, subject: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((config.from_name, config.from_email))
    # письмо адресовано одному получателю: чужие адреса не раскрываются
    message["To"] = to_email
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=config.from_email.rpartition("@")[2] or None)
    return message


def build_digest_message(
    config: SmtpConfig,
    to_email: str,
    digest: Digest,
    papers: list[PaperLine],
    pdf: bytes,
    filename: str,
) -> EmailMessage:
    """Письмо HTML + текст с кратким списком статей и PDF во вложении."""
    subject = digest_subject(digest)
    message = _new_message(config, to_email, subject)
    if papers:
        lines = "\n".join(f"{i}. {p.title}\n   {p.abs_url}" for i, p in enumerate(papers, start=1))
        text = (
            f"{subject}\n\nСтатей в выпуске: {len(papers)}. "
            f"Полные резюме — во вложенном PDF.\n\n{lines}\n"
        )
    else:
        text = f"{subject}\n\nЗа этот период подходящих статей не нашлось.\n"
    message.set_content(text)
    message.add_alternative(
        _env.get_template("email.html.j2").render(subject=subject, papers=papers), subtype="html"
    )
    message.add_attachment(pdf, maintype="application", subtype="pdf", filename=filename)
    return message


def send_test_email(
    config: SmtpConfig, to_email: str, smtp_factory: SmtpFactory = open_connection
) -> None:
    """Тестовое письмо для проверки настроек SMTP."""
    message = _new_message(config, to_email, "arXiv Digest: тестовое письмо")
    message.set_content(
        "Это тестовое письмо сервиса arXiv Digest.\n"
        "Если вы его получили, настройки SMTP работают.\n"
    )
    connection = smtp_factory(config)
    try:
        connection.send_message(message)
    finally:
        _close(connection)
    log.info("mail_test_sent", recipient=to_email)


def describe_error(exc: Exception) -> str:
    """Понятное описание ошибки SMTP для логов и поля error."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "SMTP-сервер отклонил логин или пароль"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        details = "; ".join(f"{code} {msg!r}" for code, msg in exc.recipients.values())
        return f"Сервер отклонил адрес получателя: {details}"
    if isinstance(exc, smtplib.SMTPResponseException):
        reply = exc.smtp_error
        text = reply.decode(errors="replace") if isinstance(reply, bytes) else str(reply)
        return f"SMTP {exc.smtp_code}: {text}"
    if isinstance(exc, TimeoutError):
        return "Превышено время ожидания ответа SMTP-сервера"
    if isinstance(exc, ssl.SSLError):
        return f"Ошибка TLS (проверьте режим шифрования и порт): {exc}"
    return f"{type(exc).__name__}: {exc}"


def find_digest(session: Session, digest_id: int | None) -> Digest | None:
    """Указанный дайджест либо последний со статусом ready."""
    stmt = select(Digest).options(
        selectinload(Digest.papers).selectinload(DigestPaper.paper),
        selectinload(Digest.deliveries),
    )
    if digest_id is not None:
        return session.scalar(stmt.where(Digest.id == digest_id))
    return session.scalar(
        stmt.where(Digest.status == DigestStatus.READY).order_by(
            Digest.period_end.desc(), Digest.id.desc()
        )
    )


class DigestMailer:
    def __init__(
        self,
        session: Session,
        config: SmtpConfig,
        *,
        smtp_factory: SmtpFactory = open_connection,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._session = session
        self._config = config
        self._smtp_factory = smtp_factory
        self._sleep = sleep
        self._connection: smtplib.SMTP | None = None
        # заполняется, когда к серверу не удалось подключиться за все попытки
        self._server_down: str | None = None

    def send(self, digest: Digest, *, force: bool = False) -> SendStats:
        """Рассылает дайджест активным получателям и выставляет его итоговый статус.

        Получателю, которому этот дайджест уже доставлен, письмо повторно не уходит без `force`.
        """
        stats = SendStats(digest_id=digest.id)
        if not digest.pdf_path or not Path(digest.pdf_path).is_file():
            raise MailerError(f"PDF-файл дайджеста {digest.id} не найден: соберите дайджест заново")
        pdf_path = Path(digest.pdf_path)
        pdf = pdf_path.read_bytes()
        papers = [PaperLine(item.paper.title, item.paper.abs_url) for item in digest.papers]

        emails = list(
            self._session.scalars(
                select(Recipient.email).where(Recipient.is_active.is_(True)).order_by(Recipient.id)
            )
        )
        stats.recipients_total = len(emails)
        deliveries = {d.email: d for d in digest.deliveries}
        log.info(
            "send_started",
            digest_id=digest.id,
            recipients=len(emails),
            force=force,
            pdf_size_bytes=len(pdf),
        )

        try:
            for email in emails:
                delivery = deliveries.get(email)
                if delivery is None:
                    delivery = DigestDelivery(digest_id=digest.id, email=email)
                    self._session.add(delivery)
                elif delivery.status == DeliveryStatus.SENT and not force:
                    stats.skipped_already_sent += 1
                    log.info("mail_skipped_already_sent", recipient=email, digest_id=digest.id)
                    continue
                delivery.status = DeliveryStatus.PENDING
                self._session.commit()

                if self._server_down is not None:
                    # сервер недоступен: остальных получателей не перебираем с паузами впустую
                    delivery.status = DeliveryStatus.FAILED
                    delivery.error = self._server_down
                    self._session.commit()
                    stats.failed += 1
                    continue

                message = build_digest_message(
                    self._config, email, digest, papers, pdf, pdf_path.name
                )
                if self._deliver(delivery, message):
                    stats.sent += 1
                else:
                    stats.failed += 1
        finally:
            _close(self._connection)
            self._connection = None

        delivered = stats.sent + stats.skipped_already_sent
        if stats.recipients_total == 0:
            log.warning("send_no_recipients", digest_id=digest.id)
        elif delivered == stats.recipients_total:
            digest.status = DigestStatus.SENT
        elif delivered == 0:
            digest.status = DigestStatus.FAILED
        else:
            digest.status = DigestStatus.PARTIALLY_SENT
        self._session.commit()
        stats.digest_status = digest.status.value
        log.info("send_done", **stats.as_dict())
        return stats

    def _deliver(self, delivery: DigestDelivery, message: EmailMessage) -> bool:
        """До MAX_ATTEMPTS попыток с паузой; ошибка одного получателя не мешает остальным."""
        connect_failures = 0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            delivery.attempts += 1
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
                log.warning("mail_failed", recipient=delivery.email, attempt=attempt, error=error)
                delivery.error = error
                self._session.commit()
                # после ошибки соединение может быть в непригодном состоянии — открываем заново
                _close(self._connection)
                self._connection = None
                if attempt < MAX_ATTEMPTS:
                    self._sleep(RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)])
                continue
            delivery.status = DeliveryStatus.SENT
            delivery.error = None
            delivery.sent_at = utcnow()
            self._session.commit()
            log.info("mail_sent", recipient=delivery.email, attempt=attempt)
            return True
        delivery.status = DeliveryStatus.FAILED
        self._session.commit()
        log.error("mail_gave_up", recipient=delivery.email, attempts=MAX_ATTEMPTS)
        if connect_failures == MAX_ATTEMPTS:
            self._server_down = delivery.error
            log.error("smtp_unavailable", error=delivery.error)
        return False


@dataclass
class SendOutcome:
    """Результат шага: статистика рассылки либо причина, по которой она пропущена."""

    stats: SendStats | None = None
    skip_reason: str | None = None
    digest_id: int | None = None


def run_send_digest(
    session: Session,
    settings: Settings,
    *,
    digest_id: int | None = None,
    force: bool = False,
    smtp_factory: SmtpFactory = open_connection,
    sleep: Callable[[float], None] = time.sleep,
) -> SendOutcome:
    """Рассылает указанный (или последний готовый) дайджест с учётом правил пропуска."""
    digest = find_digest(session, digest_id)
    if digest is None:
        if digest_id is not None:
            raise MailerError(f"Дайджест {digest_id} не найден")
        return SendOutcome(skip_reason="Нет дайджеста со статусом ready: отправлять нечего")
    if digest.status == DigestStatus.BUILDING:
        raise MailerError(f"Дайджест {digest.id} ещё собирается")
    if digest.status == DigestStatus.EMPTY and not settings.send_empty_digest and not force:
        log.info("send_skipped_empty_digest", digest_id=digest.id)
        return SendOutcome(
            skip_reason="Дайджест пуст, рассылка пустых дайджестов отключена (SEND_EMPTY_DIGEST)",
            digest_id=digest.id,
        )

    config = load_smtp_config(session, settings)
    stats = DigestMailer(session, config, smtp_factory=smtp_factory, sleep=sleep).send(
        digest, force=force
    )
    if stats.recipients_total == 0:
        return SendOutcome(stats=stats, skip_reason="Нет активных получателей", digest_id=digest.id)
    if stats.sent + stats.skipped_already_sent == 0:
        errors = session.scalars(
            select(DigestDelivery.error).where(
                DigestDelivery.digest_id == digest.id, DigestDelivery.error.is_not(None)
            )
        ).all()
        raise MailerError(
            f"Не удалось доставить дайджест ни одному из {stats.recipients_total} получателей: "
            f"{errors[0] if errors else 'причина неизвестна'}",
            stats=stats.as_dict(),
        )
    return SendOutcome(stats=stats, digest_id=digest.id)
