import smtplib
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import smtp as smtp_api
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.models import (
    SMTP_SETTINGS_ID,
    DeliveryStatus,
    Digest,
    DigestDelivery,
    DigestPaper,
    DigestStatus,
    PaperStatus,
    Recipient,
    RunStatus,
    RunTrigger,
    SmtpSecurity,
    SmtpSettings,
    TaskLog,
    TaskType,
)
from app.services import runs
from app.services.mailer import (
    DigestMailer,
    MailerConfigError,
    MailerError,
    SmtpConfig,
    build_digest_message,
    digest_subject,
    load_smtp_config,
    open_connection,
    run_send_digest,
    send_test_email,
)
from app.tasks.pipeline import send_digest_task
from tests.conftest import PROFILE_ID
from tests.factories import PERIOD_END, PERIOD_START
from tests.test_summarization import make_paper

CONFIG = SmtpConfig(
    host="smtp.test",
    port=587,
    username="mailer",
    password="smtp-secret",
    from_email="digest@example.com",
    from_name="arXiv Дайджест",
    security=SmtpSecurity.STARTTLS,
    timeout=10,
)
PDF = b"%PDF-1.7 fake digest"


class FakeSMTP:
    """SMTP-сервер-заглушка: отказы задаются по адресу получателя."""

    def __init__(self, failures: dict[str, int] | None = None) -> None:
        # email -> сколько раз отказать (-1 — всегда)
        self.failures = dict(failures or {})
        self.sent: list[EmailMessage] = []
        self.connections = 0
        self.closed = 0

    def __call__(self, config: SmtpConfig) -> "FakeSMTP":
        self.connections += 1
        return self

    def send_message(self, message: EmailMessage) -> None:
        recipient = message["To"]
        left = self.failures.get(recipient, 0)
        if left:
            self.failures[recipient] = left - 1
            raise smtplib.SMTPRecipientsRefused({recipient: (550, b"User unknown")})
        self.sent.append(message)

    def quit(self) -> None:
        self.closed += 1

    def close(self) -> None:
        self.closed += 1

    @property
    def recipients(self) -> list[str]:
        return [message["To"] for message in self.sent]


def _recipients(db: Session, *emails: str, inactive: tuple[str, ...] = ()) -> None:
    db.add_all([Recipient(profile_id=PROFILE_ID, email=email) for email in emails])
    db.add_all(
        [Recipient(profile_id=PROFILE_ID, email=email, is_active=False) for email in inactive]
    )
    db.commit()


def _smtp_settings(db: Session, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "host": "smtp.test",
        "port": 587,
        "username": "mailer",
        "from_email": "digest@example.com",
        "from_name": "arXiv Дайджест",
        "security": SmtpSecurity.STARTTLS,
        "timeout_seconds": 10,
    }
    values.update(overrides)
    db.add(SmtpSettings(id=SMTP_SETTINGS_ID, **values))
    db.commit()


def _digest(
    db: Session, tmp_path: Path, status: DigestStatus = DigestStatus.READY, papers: int = 2
) -> Digest:
    pdf_path = tmp_path / "digest_2024-10-05.pdf"
    pdf_path.write_bytes(PDF)
    digest = Digest(
        profile_id=PROFILE_ID,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        pdf_path=str(pdf_path),
        papers_count=papers,
        status=status,
    )
    db.add(digest)
    db.flush()
    for index in range(1, papers + 1):
        paper = make_paper(
            db, f"2410.0000{index}", PaperStatus.SUMMARIZED, title=f"Paper number {index}"
        )
        db.add(DigestPaper(digest_id=digest.id, paper_id=paper.id, position=index))
    db.commit()
    return digest


def _send(
    db: Session, digest: Digest, smtp: FakeSMTP, sleeps: list[float] | None = None, **kwargs: Any
) -> Any:
    mailer = DigestMailer(
        db,
        CONFIG,
        timezone="Europe/Moscow",
        smtp_factory=smtp,
        sleep=(sleeps if sleeps is not None else []).append,
    )
    db.refresh(digest)
    return mailer.send(digest, **kwargs)


def _deliveries(db: Session, digest: Digest) -> dict[str, DigestDelivery]:
    db.expire_all()
    rows = db.scalars(select(DigestDelivery).where(DigestDelivery.digest_id == digest.id)).all()
    return {row.email: row for row in rows}


# --- письмо ---


def test_message_has_subject_text_html_and_pdf_attachment(db: Session, tmp_path: Path) -> None:
    digest = _digest(db, tmp_path)
    smtp = FakeSMTP()
    _recipients(db, "a@example.com")

    _send(db, digest, smtp)

    message = smtp.sent[0]
    # тема — профиль и дата рассылки, период дайджеста — в теле письма
    sent_on = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y")
    assert message["Subject"] == f"arXiv Digest - Основной - {sent_on}"
    assert message["To"] == "a@example.com"
    assert "digest@example.com" in message["From"]
    assert message["Message-ID"].endswith("@example.com>")
    assert message["Date"]

    text = message.get_body(preferencelist=("plain",))
    html = message.get_body(preferencelist=("html",))
    assert text is not None and html is not None
    plain = text.get_content()
    assert plain.startswith("Дайджест arXiv за 05.10.2024\n")
    assert "Статей в выпуске: 2" in plain
    assert "1. Paper number 1" in plain
    assert "https://arxiv.org/abs/2410.00002" in plain
    rendered = html.get_content()
    assert "Дайджест arXiv за 05.10.2024" in rendered
    assert "Paper number 2" in rendered
    assert 'href="https://arxiv.org/abs/2410.00001"' in rendered

    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_content_type() == "application/pdf"
    assert attachments[0].get_filename() == "digest_Основной_2024-10-05.pdf"
    assert attachments[0].get_content() == PDF


def test_subject_date_is_in_service_timezone() -> None:
    # 22:30 UTC — в Москве уже следующий день
    sent_at = datetime(2024, 10, 5, 22, 30, tzinfo=UTC)

    assert digest_subject("Беспилотники", sent_at, ZoneInfo("Europe/Moscow")) == (
        "arXiv Digest - Беспилотники - 06.10.2024"
    )
    assert digest_subject("Беспилотники", sent_at, ZoneInfo("UTC")) == (
        "arXiv Digest - Беспилотники - 05.10.2024"
    )


def test_multi_day_title_and_empty_digest_text(db: Session, tmp_path: Path) -> None:
    digest = _digest(db, tmp_path, papers=0)
    digest.period_end = PERIOD_END.replace(day=7)

    message = build_digest_message(
        CONFIG, "a@example.com", digest, [], PDF, "digest.pdf", "arXiv Digest - Тема"
    )

    assert message["Subject"] == "arXiv Digest - Тема"
    body = message.get_body(preferencelist=("plain",))
    assert body is not None
    assert body.get_content().startswith("Дайджест arXiv за 05.10.2024 — 07.10.2024\n")
    assert "подходящих статей не нашлось" in body.get_content()


# --- рассылка ---


def test_each_recipient_gets_own_message(db: Session, tmp_path: Path) -> None:
    digest = _digest(db, tmp_path)
    _recipients(db, "a@example.com", "b@example.com", inactive=("off@example.com",))
    smtp = FakeSMTP()

    stats = _send(db, digest, smtp)

    assert smtp.recipients == ["a@example.com", "b@example.com"]
    # адреса других получателей в письме не раскрываются
    for message in smtp.sent:
        assert "," not in message["To"]
        assert message["Cc"] is None and message["Bcc"] is None
    assert smtp.connections == 1
    assert smtp.closed == 1

    assert stats.as_dict() == {
        "digest_id": digest.id,
        "recipients_total": 2,
        "sent": 2,
        "failed": 0,
        "skipped_already_sent": 0,
        "digest_status": "sent",
    }
    db.refresh(digest)
    assert digest.status == DigestStatus.SENT
    deliveries = _deliveries(db, digest)
    assert set(deliveries) == {"a@example.com", "b@example.com"}
    for delivery in deliveries.values():
        assert delivery.status == DeliveryStatus.SENT
        assert delivery.attempts == 1
        assert delivery.sent_at is not None
        assert delivery.error is None


def test_partial_failure_gives_partially_sent(db: Session, tmp_path: Path) -> None:
    digest = _digest(db, tmp_path)
    _recipients(db, "a@example.com", "bad@example.com", "c@example.com")
    smtp = FakeSMTP({"bad@example.com": -1})
    sleeps: list[float] = []

    stats = _send(db, digest, smtp, sleeps)

    assert smtp.recipients == ["a@example.com", "c@example.com"]
    assert (stats.sent, stats.failed) == (2, 1)
    assert stats.digest_status == "partially_sent"
    assert sleeps == [5.0, 15.0]
    db.refresh(digest)
    assert digest.status == DigestStatus.PARTIALLY_SENT

    deliveries = _deliveries(db, digest)
    bad = deliveries["bad@example.com"]
    assert bad.status == DeliveryStatus.FAILED
    assert bad.attempts == 3
    assert bad.sent_at is None
    assert bad.error and "550" in bad.error and "отклонил адрес" in bad.error
    assert deliveries["c@example.com"].status == DeliveryStatus.SENT


def test_transient_error_is_retried(db: Session, tmp_path: Path) -> None:
    digest = _digest(db, tmp_path)
    _recipients(db, "flaky@example.com")
    smtp = FakeSMTP({"flaky@example.com": 2})
    sleeps: list[float] = []

    stats = _send(db, digest, smtp, sleeps)

    assert stats.sent == 1
    delivery = _deliveries(db, digest)["flaky@example.com"]
    assert delivery.status == DeliveryStatus.SENT
    assert delivery.attempts == 3
    assert delivery.error is None
    assert sleeps == [5.0, 15.0]
    # после каждой ошибки соединение открывается заново
    assert smtp.connections == 3


def test_digest_is_not_sent_twice_to_the_same_recipient(db: Session, tmp_path: Path) -> None:
    digest = _digest(db, tmp_path)
    _recipients(db, "a@example.com", "bad@example.com")
    smtp = FakeSMTP({"bad@example.com": 3})
    _send(db, digest, smtp)
    assert smtp.recipients == ["a@example.com"]

    # повторный запуск: первому письмо уже доставлено, второму — досылается
    stats = _send(db, digest, smtp)

    assert smtp.recipients == ["a@example.com", "bad@example.com"]
    assert (stats.sent, stats.skipped_already_sent, stats.failed) == (1, 1, 0)
    assert stats.digest_status == "sent"
    assert _deliveries(db, digest)["bad@example.com"].attempts == 4

    third = _send(db, digest, smtp)
    assert len(smtp.sent) == 2
    assert (third.sent, third.skipped_already_sent) == (0, 2)
    assert third.digest_status == "sent"


def test_force_resends_to_everyone(db: Session, tmp_path: Path) -> None:
    digest = _digest(db, tmp_path)
    _recipients(db, "a@example.com", "b@example.com")
    smtp = FakeSMTP()
    _send(db, digest, smtp)

    stats = _send(db, digest, smtp, force=True)

    assert len(smtp.sent) == 4
    assert (stats.sent, stats.skipped_already_sent) == (2, 0)
    assert _deliveries(db, digest)["a@example.com"].attempts == 2


def test_unreachable_server_does_not_retry_every_recipient(db: Session, tmp_path: Path) -> None:
    digest = _digest(db, tmp_path)
    _recipients(db, "a@example.com", "b@example.com", "c@example.com")
    sleeps: list[float] = []
    attempts: list[int] = []

    def refuse(config: SmtpConfig) -> Any:
        attempts.append(1)
        raise ConnectionRefusedError(111, "Connection refused")

    mailer = DigestMailer(db, CONFIG, smtp_factory=refuse, sleep=sleeps.append)
    stats = mailer.send(digest)

    assert (stats.sent, stats.failed) == (0, 3)
    assert stats.digest_status == "failed"
    # три попытки подключения для первого получателя, остальным — сразу отказ
    assert len(attempts) == 3
    assert sleeps == [5.0, 15.0]
    deliveries = _deliveries(db, digest)
    assert deliveries["a@example.com"].attempts == 3
    assert deliveries["c@example.com"].attempts == 0
    assert all(d.status == DeliveryStatus.FAILED for d in deliveries.values())
    assert all(d.error and "Connection refused" in d.error for d in deliveries.values())


def test_missing_pdf_is_an_error(db: Session, tmp_path: Path) -> None:
    digest = _digest(db, tmp_path)
    _recipients(db, "a@example.com")
    Path(digest.pdf_path or "").unlink()

    with pytest.raises(MailerError, match="PDF-файл дайджеста"):
        _send(db, digest, FakeSMTP())


# --- шаг целиком: выбор дайджеста и правила пропуска ---


def _run(db: Session, smtp: FakeSMTP, settings: Settings | None = None, **kwargs: Any) -> Any:
    return run_send_digest(
        db,
        settings or get_settings(),
        profile_id=PROFILE_ID,
        smtp_factory=smtp,
        sleep=lambda _: None,
        **kwargs,
    )


def test_latest_ready_digest_is_sent_by_default(db: Session, tmp_path: Path) -> None:
    _smtp_settings(db)
    _recipients(db, "a@example.com")
    digest = _digest(db, tmp_path)
    smtp = FakeSMTP()

    outcome = _run(db, smtp)

    assert outcome.skip_reason is None
    assert outcome.digest_id == digest.id
    assert outcome.stats.sent == 1
    assert smtp.recipients == ["a@example.com"]


def test_nothing_to_send_is_skipped(db: Session, tmp_path: Path) -> None:
    _smtp_settings(db)
    _recipients(db, "a@example.com")
    smtp = FakeSMTP()

    assert "Нет дайджеста" in (_run(db, smtp).skip_reason or "")

    _digest(db, tmp_path, status=DigestStatus.SENT)
    assert "Нет дайджеста" in (_run(db, smtp).skip_reason or "")
    assert smtp.sent == []

    with pytest.raises(MailerError, match="не найден"):
        _run(db, smtp, digest_id=999)


def test_empty_digest_is_skipped_unless_enabled(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _smtp_settings(db)
    _recipients(db, "a@example.com")
    digest = _digest(db, tmp_path, status=DigestStatus.EMPTY, papers=0)
    smtp = FakeSMTP()

    skipped = _run(db, smtp, digest_id=digest.id)
    assert skipped.skip_reason and "SEND_EMPTY_DIGEST" in skipped.skip_reason
    assert smtp.sent == []

    monkeypatch.setenv("SEND_EMPTY_DIGEST", "true")
    sent = _run(db, smtp, Settings(), digest_id=digest.id)
    assert sent.skip_reason is None
    assert len(smtp.sent) == 1


def test_no_active_recipients_is_skipped(db: Session, tmp_path: Path) -> None:
    _smtp_settings(db)
    _recipients(db, inactive=("off@example.com",))
    digest = _digest(db, tmp_path)

    outcome = _run(db, FakeSMTP())

    assert outcome.skip_reason == "Нет активных получателей"
    db.refresh(digest)
    assert digest.status == DigestStatus.READY


def test_all_failed_raises_with_stats(db: Session, tmp_path: Path) -> None:
    _smtp_settings(db)
    _recipients(db, "bad@example.com")
    digest = _digest(db, tmp_path)

    with pytest.raises(MailerError) as exc_info:
        _run(db, FakeSMTP({"bad@example.com": -1}))

    assert "ни одному из 1 получателей" in str(exc_info.value)
    assert "550" in str(exc_info.value)
    assert exc_info.value.stats is not None and exc_info.value.stats["failed"] == 1
    db.refresh(digest)
    assert digest.status == DigestStatus.FAILED


# --- настройки SMTP ---


def test_config_combines_db_settings_and_env_password(db: Session) -> None:
    _smtp_settings(db, security=SmtpSecurity.SSL, port=465)

    config = load_smtp_config(db, get_settings())

    assert config.host == "smtp.test"
    assert config.port == 465
    assert config.security == SmtpSecurity.SSL
    assert config.password == "test-smtp-password"
    assert config.from_name == "arXiv Дайджест"


def test_config_errors(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(MailerConfigError, match="SMTP не настроен"):
        load_smtp_config(db, get_settings())

    _smtp_settings(db)
    monkeypatch.setenv("SMTP_PASSWORD", "")
    with pytest.raises(MailerConfigError, match="SMTP_PASSWORD"):
        load_smtp_config(db, Settings())


def test_config_without_login_does_not_need_password(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _smtp_settings(db, username="", security=SmtpSecurity.NONE, port=1025)
    monkeypatch.setenv("SMTP_PASSWORD", "")

    config = load_smtp_config(db, Settings())

    assert config.username == "" and config.password == ""


class RecordingSMTP:
    instances: ClassVar[list["RecordingSMTP"]] = []

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        self.kind = type(self).__name__
        self.host, self.port, self.kwargs = host, port, kwargs
        self.calls: list[Any] = []
        RecordingSMTP.instances.append(self)

    def ehlo(self) -> None:
        self.calls.append("ehlo")

    def starttls(self, context: Any = None) -> None:
        self.calls.append("starttls")

    def login(self, username: str, password: str) -> None:
        self.calls.append(("login", username, password))

    def send_message(self, message: EmailMessage) -> None:
        self.calls.append(("send", message["To"], message["Subject"]))

    def quit(self) -> None:
        self.calls.append("quit")

    def close(self) -> None:
        self.calls.append("close")


class RecordingSMTPSSL(RecordingSMTP):
    pass


@pytest.fixture
def recording_smtp(monkeypatch: pytest.MonkeyPatch) -> list[RecordingSMTP]:
    RecordingSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", RecordingSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", RecordingSMTPSSL)
    return RecordingSMTP.instances


@pytest.mark.parametrize(
    ("security", "username", "expected_kind", "expected_calls"),
    [
        (SmtpSecurity.NONE, "", "RecordingSMTP", ["ehlo"]),
        (
            SmtpSecurity.STARTTLS,
            "mailer",
            "RecordingSMTP",
            ["ehlo", "starttls", "ehlo", ("login", "mailer", "smtp-secret")],
        ),
        (
            SmtpSecurity.SSL,
            "mailer",
            "RecordingSMTPSSL",
            ["ehlo", ("login", "mailer", "smtp-secret")],
        ),
    ],
)
def test_connection_modes(
    recording_smtp: list[RecordingSMTP],
    security: SmtpSecurity,
    username: str,
    expected_kind: str,
    expected_calls: list[Any],
) -> None:
    config = SmtpConfig(
        host="smtp.test",
        port=2525,
        username=username,
        password="smtp-secret",
        from_email="digest@example.com",
        from_name="",
        security=security,
        timeout=7,
    )

    open_connection(config)

    connection = recording_smtp[0]
    assert connection.kind == expected_kind
    assert (connection.host, connection.port) == ("smtp.test", 2525)
    assert connection.kwargs["timeout"] == 7
    assert connection.calls == expected_calls


def test_send_test_email(recording_smtp: list[RecordingSMTP]) -> None:
    send_test_email(CONFIG, "admin@example.com")

    calls = recording_smtp[0].calls
    assert ("send", "admin@example.com", "arXiv Digest: тестовое письмо") in calls
    assert calls[-1] == "quit"


# --- задача и API ---


def test_send_digest_task(db: Session, tmp_path: Path, recording_smtp: list[RecordingSMTP]) -> None:
    configure_logging("worker")
    _smtp_settings(db)
    _recipients(db, "a@example.com", "b@example.com")
    digest = _digest(db, tmp_path)
    run = runs.create_run(
        db, profile_id=PROFILE_ID, task_type=TaskType.SEND_DIGEST, trigger=RunTrigger.MANUAL
    )
    db.commit()

    result = send_digest_task.apply(args=[{"digest_id": digest.id}], kwargs={"run_id": run.id})

    assert result.successful()
    db.refresh(run)
    assert run.status == RunStatus.SUCCESS
    assert run.stats["sent"] == 2
    assert run.stats["digest_status"] == "sent"
    sends = [call for call in recording_smtp[0].calls if call[0] == "send"]
    assert [call[1] for call in sends] == ["a@example.com", "b@example.com"]

    logs = db.scalars(select(TaskLog).where(TaskLog.run_id == run.id)).all()
    messages = [entry.message for entry in logs]
    assert "smtp_connect" in messages
    assert messages.count("mail_sent") == 2
    # пароль SMTP в логи задачи не попадает
    secret = get_settings().smtp_password
    assert secret is not None
    assert all(secret.get_secret_value() not in str(entry.extra) for entry in logs)


def test_send_digest_task_is_skipped_for_empty_digest(
    db: Session, tmp_path: Path, recording_smtp: list[RecordingSMTP]
) -> None:
    configure_logging("worker")
    _smtp_settings(db)
    _recipients(db, "a@example.com")
    digest = _digest(db, tmp_path, status=DigestStatus.EMPTY, papers=0)
    run = runs.create_run(
        db, profile_id=PROFILE_ID, task_type=TaskType.SEND_DIGEST, trigger=RunTrigger.MANUAL
    )
    db.commit()

    send_digest_task.apply(args=[{"digest_id": digest.id}], kwargs={"run_id": run.id})

    db.refresh(run)
    assert run.status == RunStatus.SKIPPED
    assert run.error and "SEND_EMPTY_DIGEST" in run.error
    assert recording_smtp == []


def test_send_digest_task_fails_without_smtp_settings(db: Session, tmp_path: Path) -> None:
    configure_logging("worker")
    _recipients(db, "a@example.com")
    digest = _digest(db, tmp_path)
    run = runs.create_run(
        db, profile_id=PROFILE_ID, task_type=TaskType.SEND_DIGEST, trigger=RunTrigger.MANUAL
    )
    db.commit()

    send_digest_task.apply(args=[{"digest_id": digest.id}], kwargs={"run_id": run.id})

    db.refresh(run)
    assert run.status == RunStatus.FAILED
    assert run.error and "SMTP не настроен" in run.error


async def test_smtp_test_endpoint(
    auth_client: AsyncClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    not_configured = await auth_client.post("/api/smtp/test", json={"to": "admin@example.com"})
    assert not_configured.status_code == 400
    assert not_configured.json()["code"] == "smtp_not_configured"

    _smtp_settings(db)
    sent: list[str] = []
    monkeypatch.setattr(smtp_api, "send_test_email", lambda config, to: sent.append(to))
    ok = await auth_client.post("/api/smtp/test", json={"to": "admin@example.com"})
    assert ok.status_code == 200
    assert ok.json() == {"ok": True, "to": "admin@example.com"}
    assert sent == ["admin@example.com"]

    def refuse(config: SmtpConfig, to: str) -> None:
        raise smtplib.SMTPAuthenticationError(535, b"Bad credentials")

    monkeypatch.setattr(smtp_api, "send_test_email", refuse)
    failed = await auth_client.post("/api/smtp/test", json={"to": "admin@example.com"})
    assert failed.status_code == 400
    assert failed.json()["code"] == "smtp_error"
    assert "отклонил логин или пароль" in failed.json()["detail"]

    invalid = await auth_client.post("/api/smtp/test", json={"to": "not-an-email"})
    assert invalid.status_code == 422


async def test_resend_endpoint(
    auth_client: AsyncClient, db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    published: list[Any] = []
    monkeypatch.setattr(
        send_digest_task,
        "apply_async",
        lambda args=None, kwargs=None, **options: published.append(args),
    )
    digest = _digest(db, tmp_path, status=DigestStatus.SENT)

    forced = await auth_client.post(f"/api/digests/{digest.id}/resend")
    assert forced.status_code == 202
    assert forced.json()["task_type"] == "send_digest"
    assert forced.json()["params"] == {"digest_id": digest.id, "force": True}

    missing_only = await auth_client.post(f"/api/digests/{digest.id}/resend", json={"force": False})
    assert missing_only.json()["params"] == {"digest_id": digest.id, "force": False}
    assert published == [
        [{"digest_id": digest.id, "force": True}],
        [{"digest_id": digest.id, "force": False}],
    ]

    Path(digest.pdf_path or "").unlink()
    no_pdf = await auth_client.post(f"/api/digests/{digest.id}/resend")
    assert no_pdf.status_code == 409
    assert no_pdf.json()["code"] == "no_pdf"
    assert (await auth_client.post("/api/digests/999999/resend")).status_code == 404
