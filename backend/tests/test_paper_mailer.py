from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Paper, Recipient, Translation, TranslationStatus
from app.services.mailer import MailerConfigError, MailerError
from app.services.paper_mailer import run_send_paper
from app.tasks.pipeline import send_paper_task
from tests.conftest import PROFILE_ID
from tests.test_mailer import FakeSMTP, _smtp_settings
from tests.test_translations_api import make_paper

PDF = b"%PDF-1.7 fake paper"
DOCX = b"PK fake docx"


def _paper(db: Session, tmp_path: Path, *, with_pdf: bool = True) -> Paper:
    pdf_path = tmp_path / "papers" / "2610.00001.pdf"
    if with_pdf:
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(PDF)
    return make_paper(db, pdf_path=pdf_path if with_pdf else None)


def _translation(
    db: Session, tmp_path: Path, paper: Paper, language: str = "ru", *, with_file: bool = True
) -> Translation:
    path = tmp_path / "translations" / f"{paper.arxiv_id}_{language}.docx"
    if with_file:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(DOCX)
    translation = Translation(
        paper_id=paper.id,
        language=language,
        status=TranslationStatus.READY if with_file else TranslationStatus.RUNNING,
        docx_path=str(path) if with_file else None,
    )
    db.add(translation)
    db.commit()
    return translation


def _recipients(db: Session, *emails: str) -> list[int]:
    rows = [Recipient(profile_id=PROFILE_ID, email=email) for email in emails]
    db.add_all(rows)
    db.commit()
    return [row.id for row in rows]


def _send(db: Session, smtp: FakeSMTP, sleeps: list[float] | None = None, **kwargs: Any) -> Any:
    return run_send_paper(
        db,
        get_settings(),
        smtp_factory=smtp,  # type: ignore[arg-type]
        sleep=(sleeps if sleeps is not None else []).append,
        **kwargs,
    )


def test_selected_documents_go_to_selected_recipients(db: Session, tmp_path: Path) -> None:
    _smtp_settings(db)
    paper = _paper(db, tmp_path)
    russian = _translation(db, tmp_path, paper, "ru")
    _translation(db, tmp_path, paper, "en")
    first, _, third = _recipients(db, "a@example.com", "b@example.com", "c@example.com")
    smtp = FakeSMTP()

    stats = _send(
        db,
        smtp,
        paper_id=paper.id,
        recipient_ids=[third, first],
        include_pdf=True,
        translation_ids=[russian.id],
    )

    # письмо уходит только выбранным получателям, каждому отдельно
    assert smtp.recipients == ["a@example.com", "c@example.com"]
    assert smtp.connections == 1
    assert stats.as_dict() == {
        "paper_id": paper.id,
        "arxiv_id": "2610.00001",
        "recipients_total": 2,
        "sent": 2,
        "failed": 0,
        "attachments": ["2610.00001.pdf", "2610.00001_ru.docx"],
        "attachments_size_bytes": len(PDF) + len(DOCX),
        "errors": {},
    }
    message = smtp.sent[0]
    # тема — название статьи на языке оригинала, без номера arXiv
    assert message["Subject"] == paper.title == "Roll Prediction for Surface Vehicles"
    attachments = {part.get_filename(): part for part in message.iter_attachments()}
    assert set(attachments) == {"2610.00001.pdf", "2610.00001_ru.docx"}
    assert attachments["2610.00001.pdf"].get_content() == PDF
    assert attachments["2610.00001.pdf"].get_content_type() == "application/pdf"
    assert attachments["2610.00001_ru.docx"].get_content() == DOCX
    body = message.get_body(("plain",)).get_content()  # type: ignore[union-attr]
    assert paper.abs_url in body
    assert "перевод, Русский (docx): 2610.00001_ru.docx" in body
    html = message.get_body(("html",)).get_content()  # type: ignore[union-attr]
    assert paper.title in html and "исходная статья (PDF)" in html


def test_only_translation_can_be_sent(db: Session, tmp_path: Path) -> None:
    _smtp_settings(db)
    paper = _paper(db, tmp_path, with_pdf=False)
    translation = _translation(db, tmp_path, paper)
    (recipient,) = _recipients(db, "a@example.com")
    smtp = FakeSMTP()

    stats = _send(
        db,
        smtp,
        paper_id=paper.id,
        recipient_ids=[recipient],
        include_pdf=False,
        translation_ids=[translation.id],
    )

    assert stats.attachments == ["2610.00001_ru.docx"]
    assert [part.get_filename() for part in smtp.sent[0].iter_attachments()] == [
        "2610.00001_ru.docx"
    ]


def test_failed_recipient_does_not_stop_the_rest(db: Session, tmp_path: Path) -> None:
    _smtp_settings(db)
    paper = _paper(db, tmp_path)
    ids = _recipients(db, "bad@example.com", "flaky@example.com", "ok@example.com")
    smtp = FakeSMTP({"bad@example.com": -1, "flaky@example.com": 1})
    sleeps: list[float] = []

    stats = _send(
        db, smtp, sleeps, paper_id=paper.id, recipient_ids=ids, include_pdf=True, translation_ids=[]
    )

    assert sorted(smtp.recipients) == ["flaky@example.com", "ok@example.com"]
    assert (stats.sent, stats.failed) == (2, 1)
    assert list(stats.errors) == ["bad@example.com"]
    assert "отклонил адрес получателя" in stats.errors["bad@example.com"]
    # три попытки для недоставленного и одна повторная для временного отказа
    assert sleeps == [5.0, 15.0, 5.0]


def test_nothing_delivered_is_an_error_with_stats(db: Session, tmp_path: Path) -> None:
    _smtp_settings(db)
    paper = _paper(db, tmp_path)
    ids = _recipients(db, "bad@example.com")

    with pytest.raises(MailerError, match="ни одному из 1 получателей") as error:
        _send(
            db,
            FakeSMTP({"bad@example.com": -1}),
            paper_id=paper.id,
            recipient_ids=ids,
            include_pdf=True,
            translation_ids=[],
        )

    assert error.value.stats is not None
    assert (error.value.stats["sent"], error.value.stats["failed"]) == (0, 1)


def test_missing_files_recipients_and_smtp_are_reported(db: Session, tmp_path: Path) -> None:
    paper = _paper(db, tmp_path, with_pdf=False)
    pending = _translation(db, tmp_path, paper, with_file=False)
    ids = _recipients(db, "a@example.com")
    smtp = FakeSMTP()

    def send(**kwargs: Any) -> Any:
        params: dict[str, Any] = {
            "paper_id": paper.id,
            "recipient_ids": ids,
            "include_pdf": False,
            "translation_ids": [],
        }
        return _send(db, smtp, **{**params, **kwargs})

    with pytest.raises(MailerError, match="PDF-файл статьи не найден"):
        send(include_pdf=True)
    with pytest.raises(MailerError, match=r"Файл перевода \(Русский\) не найден"):
        send(translation_ids=[pending.id])
    with pytest.raises(MailerError, match="Переводы не найдены"):
        send(translation_ids=[999_999])
    with pytest.raises(MailerError, match="ни одного документа"):
        send()
    with pytest.raises(MailerError, match="получатели не найдены"):
        send(recipient_ids=[999_999], include_pdf=True)
    with pytest.raises(MailerError, match="Статья 999999 не найдена"):
        send(paper_id=999_999)

    # SMTP не настроен — понятная ошибка, а не попытка соединения
    ready = _translation(db, tmp_path, paper, "en")
    with pytest.raises(MailerConfigError, match="SMTP не настроен"):
        send(translation_ids=[ready.id])
    assert smtp.connections == 0


async def test_send_endpoint_validates_and_enqueues(
    auth_client: AsyncClient, db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    published: list[dict[str, Any]] = []
    monkeypatch.setattr(
        send_paper_task,
        "apply_async",
        lambda args=None, kwargs=None, **options: published.append({"args": args}),
    )
    paper = _paper(db, tmp_path)
    no_pdf = make_paper(db, "2610.00002")
    ready = _translation(db, tmp_path, paper, "ru")
    pending = _translation(db, tmp_path, paper, "en", with_file=False)
    first, second = _recipients(db, "a@example.com", "b@example.com")
    url = f"/api/papers/{paper.id}/send"

    detail = (await auth_client.get(f"/api/papers/{paper.id}")).json()
    assert detail["has_pdf"] is True
    assert (await auth_client.get(f"/api/papers/{no_pdf.id}")).json()["has_pdf"] is False

    response = await auth_client.post(
        url,
        json={
            "recipient_ids": [second, first, first],
            "include_pdf": True,
            "translation_ids": [ready.id],
        },
    )

    assert response.status_code == 202
    run = response.json()
    params = {
        "paper_id": paper.id,
        "recipient_ids": [first, second],
        "include_pdf": True,
        "translation_ids": [ready.id],
    }
    assert (run["task_type"], run["status"], run["params"]) == ("send_paper", "queued", params)
    assert published == [{"args": [params]}]

    async def post(target: str = url, **body: Any) -> tuple[int, str]:
        result = await auth_client.post(target, json={"recipient_ids": [first], **body})
        return result.status_code, result.json()["code"]

    assert await post() == (422, "validation_error")
    assert await post(recipient_ids=[], include_pdf=True) == (422, "validation_error")
    assert await post(f"/api/papers/{no_pdf.id}/send", include_pdf=True) == (409, "no_pdf")
    assert await post(translation_ids=[pending.id]) == (409, "no_translation_file")
    assert await post(translation_ids=[999_999]) == (404, "not_found")
    assert await post(recipient_ids=[999_999], include_pdf=True) == (404, "not_found")
    assert await post("/api/papers/999999/send", include_pdf=True) == (404, "not_found")
    assert len(published) == 1

    # как и перевод, отправка не запускается вручную и по расписанию
    launch = await auth_client.post("/api/runs", json={"task_type": "send_paper"})
    assert launch.status_code == 422
    schedule = await auth_client.post(
        "/api/schedules",
        json={"name": "Отправка", "task_type": "send_paper", "cron_expression": "0 6 * * *"},
    )
    assert schedule.status_code == 422
