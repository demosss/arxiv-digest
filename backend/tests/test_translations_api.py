from pathlib import Path
from typing import Any

import pymupdf
import pytest
from docx import Document as open_docx
from httpx import AsyncClient
from sqlalchemy.orm import Session

from app.models import (
    Paper,
    PaperStatus,
    RunStatus,
    RunTrigger,
    TaskRun,
    TaskType,
    Translation,
    TranslationStatus,
)
from app.services import runs
from app.services.arxiv_client import ArxivError
from app.services.llm_client import LLMHealth, LLMUnavailableError
from app.services.translation.service import (
    TranslationError,
    fail_orphaned,
    prepare_translation,
    run_translation,
)
from app.tasks.pipeline import translate_task
from tests.conftest import PROFILE_ID
from tests.factories import RUSSIAN_PARAGRAPH, FakeTranslationLLM, make_entry
from tests.test_translation_pdf import make_paper as make_pdf

HTML = (Path(__file__).parent / "fixtures" / "arxiv_paper.html").read_bytes()


def _png() -> bytes:
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 36))
    pixmap.set_rect(pixmap.irect, (30, 90, 200))
    return pixmap.tobytes("png")


class FakeFetcher:
    """Страницы arXiv: HTML статьи (или 404, если его нет) и картинки."""

    def __init__(self, html: bytes | None = HTML) -> None:
        self.html = html
        self.urls: list[str] = []

    def fetch(self, url: str, what: str = "") -> tuple[bytes, str]:
        self.urls.append(url)
        if url.endswith(".png"):
            return _png(), url
        if self.html is None:
            raise ArxivError(f"{what}: HTTP 404 ({url})")
        return self.html, url


class DownLLM(FakeTranslationLLM):
    def health(self) -> LLMHealth:
        return LLMHealth(
            ok=False, base_url="http://llm.test/v1", model=self.model, error="нет соединения"
        )


@pytest.fixture(autouse=True)
def _short_html_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.translation.html_source.MIN_TEXT_CHARS", 100)


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Задачи перевода, поставленные в очередь (без брокера)."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        translate_task,
        "apply_async",
        lambda args=None, kwargs=None, **options: calls.append({"args": args, "kwargs": kwargs}),
    )
    return calls


def make_paper(db: Session, arxiv_id: str = "2610.00001", pdf_path: Path | None = None) -> Paper:
    entry = make_entry(arxiv_id, title="Roll Prediction for Surface Vehicles")
    paper = Paper(
        profile_id=PROFILE_ID,
        arxiv_id=entry.arxiv_id,
        version=1,
        title=entry.title,
        authors=entry.authors,
        abstract=entry.abstract,
        categories=["cs.RO"],
        published_at=entry.published_at,
        pdf_url=entry.pdf_url,
        abs_url=entry.abs_url,
        status=PaperStatus.SUMMARIZED,
        pdf_path=str(pdf_path) if pdf_path else None,
    )
    db.add(paper)
    db.commit()
    return paper


def _queued(db: Session, paper: Paper, language: str = "ru") -> tuple[Translation, TaskRun]:
    translation = prepare_translation(db, paper.id, language)
    run = runs.create_run(
        db, profile_id=PROFILE_ID, task_type=TaskType.TRANSLATE, trigger=RunTrigger.MANUAL
    )
    translation.run_id = run.id
    db.commit()
    return translation, run


def _run(
    db: Session, translation: Translation, run: TaskRun, tmp_path: Path, **kwargs: Any
) -> dict[str, Any]:
    kwargs.setdefault("llm", FakeTranslationLLM())
    kwargs.setdefault("fetcher", FakeFetcher())
    return run_translation(
        db,
        translation.id,
        run_id=run.id,
        translations_dir=tmp_path / "translations",
        chunk_chars=3000,
        max_tokens=2048,
        **kwargs,
    )


async def test_translate_endpoint_enqueues_run(
    auth_client: AsyncClient, db: Session, published: list[dict[str, Any]]
) -> None:
    paper = make_paper(db)

    response = await auth_client.post(
        f"/api/papers/{paper.id}/translate", json={"language": " Русский "}
    )

    assert response.status_code == 202
    body = response.json()
    # название языка из списка приводится к коду
    assert (body["language"], body["language_label"], body["status"]) == ("ru", "Русский", "queued")
    assert (body["has_file"], body["filename"], body["error"]) == (False, None, None)
    params = {"translation_id": body["id"], "paper_id": paper.id, "language": "ru"}
    assert published == [{"args": [params], "kwargs": {"run_id": body["run_id"]}}]
    run = (await auth_client.get(f"/api/runs/{body['run_id']}")).json()
    assert (run["task_type"], run["status"], run["params"]) == ("translate", "queued", params)

    # пока перевод выполняется, второй такой же не запускается
    conflict = await auth_client.post(f"/api/papers/{paper.id}/translate", json={"language": "ru"})
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "translation_active"
    # язык не из списка принимается как есть
    custom = await auth_client.post(
        f"/api/papers/{paper.id}/translate", json={"language": "итальянский"}
    )
    assert custom.status_code == 202
    assert custom.json()["language"] == custom.json()["language_label"] == "итальянский"
    assert len(published) == 2

    detail = (await auth_client.get(f"/api/papers/{paper.id}")).json()
    assert [item["language"] for item in detail["translations"]] == ["ru", "итальянский"]
    missing = await auth_client.post("/api/papers/999999/translate", json={"language": "ru"})
    assert missing.status_code == 404
    empty = await auth_client.post(f"/api/papers/{paper.id}/translate", json={"language": "  "})
    assert empty.status_code == 422


async def test_translation_from_html_is_built_and_downloaded(
    auth_client: AsyncClient, db: Session, tmp_path: Path
) -> None:
    paper = make_paper(db)
    translation, run = _queued(db, paper)
    fetcher = FakeFetcher()

    stats = _run(db, translation, run, tmp_path, fetcher=fetcher)

    assert fetcher.urls == [
        "https://arxiv.org/html/2610.00001v1",
        "https://arxiv.org/html/2610.00001v1/overall.png",
    ]
    assert (stats["source"], stats["language"], stats["untranslated"]) == ("html", "ru", 0)
    assert (stats["formulas"], stats["formulas_editable"], stats["figures"]) == (3, 3, 1)
    db.refresh(translation)
    assert translation.status == TranslationStatus.READY
    assert (translation.source, translation.model, translation.error) == (
        "html",
        "fake-model",
        None,
    )
    path = tmp_path / "translations" / str(PROFILE_ID) / "2610.00001_ru.docx"
    assert translation.docx_path == str(path)
    texts = [paragraph.text for paragraph in open_docx(str(path)).paragraphs]
    assert RUSSIAN_PARAGRAPH in " ".join(texts)
    # авторы — из метаданных arXiv, список литературы — на языке оригинала
    assert "Alice Smith, Bob Jones" in texts
    assert any(text.startswith("[1] Q. Zhang and B. Wu.") for text in texts)

    listing = (await auth_client.get("/api/translations", params={"q": "roll"})).json()
    assert listing["total"] == 1
    item = listing["items"][0]
    assert (item["arxiv_id"], item["title"]) == ("2610.00001", paper.title)
    assert (item["status"], item["has_file"], item["filename"]) == ("ready", True, path.name)
    assert (await auth_client.get("/api/translations", params={"status": "failed"})).json()[
        "total"
    ] == 0
    assert (await auth_client.get("/api/translations", params={"language": "English"})).json()[
        "total"
    ] == 0

    download = await auth_client.get(f"/api/translations/{translation.id}/docx")
    assert download.status_code == 200
    assert download.content[:2] == b"PK"
    assert path.name in download.headers["content-disposition"]

    assert (await auth_client.delete(f"/api/translations/{translation.id}")).status_code == 204
    assert not path.exists()
    assert (await auth_client.get(f"/api/translations/{translation.id}/docx")).status_code == 404


def test_pdf_is_used_when_html_version_is_missing(db: Session, tmp_path: Path) -> None:
    pdf_path = tmp_path / "papers" / "2610.00001.pdf"
    make_pdf(pdf_path)
    paper = make_paper(db, pdf_path=pdf_path)
    translation, run = _queued(db, paper, "en")

    stats = _run(db, translation, run, tmp_path, fetcher=FakeFetcher(html=None))

    db.refresh(translation)
    assert (translation.status, translation.source) == (TranslationStatus.READY, "pdf")
    # формула из PDF восстановлена моделью и набрана для редактора формул
    assert (stats["formulas"], stats["formulas_restored"], stats["formulas_editable"]) == (1, 1, 1)
    assert (tmp_path / "translations" / str(PROFILE_ID) / "2610.00001_en.docx").is_file()


def test_failed_translation_keeps_reason(db: Session, tmp_path: Path) -> None:
    paper = make_paper(db)
    translation, run = _queued(db, paper)

    with pytest.raises(TranslationError, match="нет HTML-версии"):
        _run(db, translation, run, tmp_path, fetcher=FakeFetcher(html=None))

    db.refresh(translation)
    assert translation.status == TranslationStatus.FAILED
    assert "PDF-файл не сохранён" in (translation.error or "")
    assert translation.docx_path is None

    # повторный запуск того же перевода разрешён; недоступная модель — тоже понятная ошибка
    translation, run = _queued(db, paper)
    with pytest.raises(LLMUnavailableError):
        _run(db, translation, run, tmp_path, llm=DownLLM())
    db.refresh(translation)
    assert translation.status == TranslationStatus.FAILED
    assert "LLM недоступна" in (translation.error or "")


def test_translation_of_cancelled_run_is_marked_failed(db: Session) -> None:
    paper = make_paper(db)
    cancelled, cancelled_run = _queued(db, paper, "ru")
    active, _ = _queued(db, paper, "en")
    runs.finish_run(db, cancelled_run, RunStatus.CANCELLED, error="Отменено пользователем")

    assert fail_orphaned(db) == [cancelled.id]
    db.commit()

    db.refresh(cancelled)
    db.refresh(active)
    assert cancelled.status == TranslationStatus.FAILED
    assert cancelled.error == "Запуск перевода отменён или прерван"
    assert active.status == TranslationStatus.QUEUED


async def test_translate_cannot_be_launched_or_scheduled(auth_client: AsyncClient) -> None:
    launch = await auth_client.post("/api/runs", json={"task_type": "translate"})
    assert launch.status_code == 422

    schedule = await auth_client.post(
        "/api/schedules",
        json={"name": "Перевод", "task_type": "translate", "cron_expression": "0 6 * * *"},
    )
    assert schedule.status_code == 422


async def test_files_are_removed_with_paper_and_on_clear(
    auth_client: AsyncClient, db: Session, tmp_path: Path
) -> None:
    first, second = make_paper(db, "2610.00001"), make_paper(db, "2610.00002")
    ready, run = _queued(db, first)
    _run(db, ready, run, tmp_path)
    other, run = _queued(db, second)
    _run(db, other, run, tmp_path)
    runs.finish_run(db, run, RunStatus.SUCCESS)
    runs.finish_run(db, db.get_one(TaskRun, ready.run_id), RunStatus.SUCCESS)
    in_progress, _ = _queued(db, second, "en")
    first_file, second_file = (Path(str(t.docx_path)) for t in (ready, other))
    assert first_file.is_file() and second_file.is_file()

    # активный запуск перевода защищает статьи от удаления, как и остальные запуски
    blocked = await auth_client.delete(f"/api/papers/{first.id}")
    assert blocked.status_code == 409
    # очистка переводов выполняющийся перевод не трогает
    cleared = await auth_client.delete("/api/translations", params={"q": "2610.00002"})
    assert cleared.json() == {"deleted": 1}
    assert not second_file.exists()
    remaining = (await auth_client.get("/api/translations")).json()
    assert {item["id"] for item in remaining["items"]} == {ready.id, in_progress.id}
    active = await auth_client.delete(f"/api/translations/{in_progress.id}")
    assert active.status_code == 409

    runs.finish_run(db, db.get_one(TaskRun, in_progress.run_id), RunStatus.CANCELLED)
    assert (await auth_client.delete(f"/api/papers/{first.id}")).status_code == 204
    assert not first_file.exists()
    assert (await auth_client.get("/api/translations")).json()["total"] == 1


async def test_languages_endpoint(auth_client: AsyncClient) -> None:
    languages = (await auth_client.get("/api/translations/languages")).json()

    assert [item["code"] for item in languages] == ["ru", "en", "de", "fr", "es"]
    assert languages[0] == {"code": "ru", "label": "Русский"}
