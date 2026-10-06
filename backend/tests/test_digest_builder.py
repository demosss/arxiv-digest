from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pymupdf
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.models import (
    Digest,
    DigestPaper,
    DigestStatus,
    Paper,
    PaperStatus,
    PaperTopic,
    RunStatus,
    RunTrigger,
    Summary,
    TaskType,
    Topic,
)
from app.services import runs
from app.services.digest_builder import (
    build_digest,
    digest_filename,
    format_authors,
    format_period,
    split_summary,
)
from app.tasks.pipeline import build_digest_task
from tests.factories import PERIOD_END, PERIOD_START, RUSSIAN_SUMMARY
from tests.test_summarization import make_paper

MOSCOW = ZoneInfo("Europe/Moscow")


def _summarized(db: Session, arxiv_id: str, title: str, topics: list[Topic]) -> Paper:
    paper = make_paper(db, arxiv_id, PaperStatus.SUMMARIZED, title=title)
    db.add(Summary(paper_id=paper.id, text_ru=RUSSIAN_SUMMARY, model="fake-model"))
    for topic in topics:
        db.add(PaperTopic(paper_id=paper.id, topic_id=topic.id, matched_keywords=[]))
    db.commit()
    return paper


@pytest.fixture
def topics(db: Session) -> tuple[Topic, Topic]:
    agents = Topic(name="Агенты", keywords=["agents", "tool use"])
    llm = Topic(name="Языковые модели", keywords=["large language model"])
    db.add_all([agents, llm])
    db.commit()
    return agents, llm


def _build(db: Session, tmp_path: Path) -> Path:
    built = build_digest(
        db, PERIOD_START, PERIOD_END, digests_dir=tmp_path / "digests", timezone="Europe/Moscow"
    )
    return built.pdf_path


def _pdf_text(path: Path) -> str:
    with pymupdf.open(path) as document:
        return "\n".join(page.get_text() for page in document)


# --- вспомогательные функции ---


def test_authors_are_limited_to_five() -> None:
    assert format_authors(["A", "B", "C"]) == "A, B, C"
    assert format_authors(["A", "B", "C", "D", "E"]) == "A, B, C, D, E"
    assert format_authors(["A", "B", "C", "D", "E", "F", "G"]) == "A, B, C, D, E и др."


def test_period_label() -> None:
    assert format_period(PERIOD_START, PERIOD_END, MOSCOW) == "05.10.2024 (UTC)"
    week_end = PERIOD_END + timedelta(days=6)
    assert format_period(PERIOD_START, week_end, MOSCOW) == "05.10.2024 — 11.10.2024 (UTC)"
    partial = format_period(
        datetime(2024, 10, 5, 6, 0, tzinfo=UTC), datetime(2024, 10, 5, 18, 30, tzinfo=UTC), MOSCOW
    )
    assert partial == "05.10.2024 09:00 — 05.10.2024 21:30 (MSK)"


def test_digest_filename() -> None:
    assert digest_filename(PERIOD_START, PERIOD_END) == "digest_2024-10-05.pdf"
    assert (
        digest_filename(PERIOD_START, PERIOD_END + timedelta(days=2))
        == "digest_2024-10-05_2024-10-07.pdf"
    )


def test_split_summary_extracts_section_labels() -> None:
    parts = split_summary("Задача: первое.\n\nметод: второе.\nПросто абзац.\n\nЗначимость: итог.")

    assert parts == [
        ("Задача", "первое."),
        ("Метод", "второе."),
        ("", "Просто абзац."),
        ("Значимость", "итог."),
    ]


# --- генерация PDF ---


def test_pdf_is_created_with_cyrillic_text(
    db: Session, tmp_path: Path, topics: tuple[Topic, Topic]
) -> None:
    agents, llm = topics
    _summarized(db, "2410.00001", "Agents That Plan", [agents])
    _summarized(db, "2410.00002", "Scaling Language Models", [llm])

    pdf_path = _build(db, tmp_path)

    assert pdf_path == tmp_path / "digests" / "digest_2024-10-05.pdf"
    assert pdf_path.read_bytes().startswith(b"%PDF-")
    text = _pdf_text(pdf_path)
    # кириллица извлекается обратно: титул, оглавление, темы, резюме
    assert "Дайджест препринтов arXiv" in text
    assert "05.10.2024 (UTC)" in text
    assert "Оглавление" in text
    assert "Агенты" in text and "Языковые модели" in text
    assert "Ключевые слова: agents, tool use" in text
    assert "Agents That Plan" in text
    assert "Задача:" in text and "Значимость:" in text
    assert "многошаговыми" in text.replace("-\n", "").replace("\n", " ")
    assert "Alice Smith, Bob Jones" in text
    assert "arXiv:2410.00001" in text
    assert "Стр. 1 из" in text


def test_cyrillic_fonts_are_embedded(
    db: Session, tmp_path: Path, topics: tuple[Topic, Topic]
) -> None:
    _summarized(db, "2410.00001", "Agents That Plan", [topics[0]])

    pdf_path = _build(db, tmp_path)

    with pymupdf.open(pdf_path) as document:
        fonts = {font for page in document for font in page.get_fonts(full=True)}
        embedded = []
        for xref, extension, _type, basefont, *_ in fonts:
            # у встроенного шрифта есть файл; у невстроенного extension == "n/a"
            assert extension != "n/a", f"шрифт {basefont} не встроен"
            assert document.extract_font(xref)[3], f"у шрифта {basefont} нет данных"
            embedded.append(basefont)
    names = " ".join(embedded)
    assert "PT-Serif" in names or "PTSerif" in names
    assert "PT-Sans" in names or "PTSans" in names


def test_pdf_has_links_to_arxiv_and_internal_toc_links(
    db: Session, tmp_path: Path, topics: tuple[Topic, Topic]
) -> None:
    agents, llm = topics
    _summarized(db, "2410.00001", "Agents That Plan", [agents])
    _summarized(db, "2410.00002", "Scaling Language Models", [llm])

    pdf_path = _build(db, tmp_path)

    with pymupdf.open(pdf_path) as document:
        links = [link for page in document for link in page.get_links()]
        pages = document.page_count
    uris = {link["uri"] for link in links if link.get("uri")}
    assert uris == {"https://arxiv.org/abs/2410.00001", "https://arxiv.org/abs/2410.00002"}
    internal = [link for link in links if link["kind"] in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED)]
    # две ссылки оглавления ведут на разделы тем
    assert len(internal) >= 2
    # титул с оглавлением и по странице на тему
    assert pages == 3


def test_paper_in_several_topics_is_printed_once_with_reference(
    db: Session, tmp_path: Path, topics: tuple[Topic, Topic]
) -> None:
    agents, llm = topics
    shared = _summarized(db, "2410.00001", "Shared Paper About Both", [llm, agents])
    _summarized(db, "2410.00002", "Only Language Models", [llm])

    built = build_digest(
        db, PERIOD_START, PERIOD_END, digests_dir=tmp_path, timezone="Europe/Moscow"
    )

    by_name = {section.name: section for section in built.sections}
    # «Агенты» идут первыми по алфавиту: там полная карточка
    assert [(i.arxiv_id, i.is_reference) for i in by_name["Агенты"].items] == [
        ("2410.00001", False)
    ]
    assert [(i.arxiv_id, i.is_reference) for i in by_name["Языковые модели"].items] == [
        ("2410.00001", True),
        ("2410.00002", False),
    ]
    text = _pdf_text(built.pdf_path)
    assert text.count("arXiv:2410.00001") == 1
    assert "см. в разделе «Агенты»" in text

    assert built.digest.papers_count == 2
    composition = db.scalars(
        select(DigestPaper).where(DigestPaper.digest_id == built.digest.id)
    ).all()
    assert [(c.position, c.paper_id, c.topic_id) for c in composition] == [
        (1, shared.id, agents.id),
        (2, shared.id + 1, llm.id),
    ]


def test_only_summarized_papers_of_the_period_are_included(
    db: Session, tmp_path: Path, topics: tuple[Topic, Topic]
) -> None:
    included = _summarized(db, "2410.00001", "Included Paper", [topics[0]])
    outside = _summarized(db, "2410.00002", "Published Later", [topics[0]])
    outside.published_at = PERIOD_END + timedelta(hours=5)
    not_ready = make_paper(db, "2410.00003", PaperStatus.TEXT_EXTRACTED, title="Not Summarized")
    db.add(PaperTopic(paper_id=not_ready.id, topic_id=topics[0].id, matched_keywords=[]))
    db.commit()

    built = build_digest(
        db, PERIOD_START, PERIOD_END, digests_dir=tmp_path, timezone="Europe/Moscow"
    )

    assert built.digest.status == DigestStatus.READY
    assert built.digest.papers_count == 1
    text = _pdf_text(built.pdf_path)
    assert included.title in text
    assert "Published Later" not in text and "Not Summarized" not in text


def test_empty_digest(db: Session, tmp_path: Path) -> None:
    built = build_digest(
        db, PERIOD_START, PERIOD_END, digests_dir=tmp_path, timezone="Europe/Moscow"
    )

    assert built.digest.status == DigestStatus.EMPTY
    assert built.digest.papers_count == 0
    assert built.sections == []
    assert "не найдено статей" in _pdf_text(built.pdf_path)
    assert "Оглавление" not in _pdf_text(built.pdf_path)


def test_rebuild_updates_the_same_digest(
    db: Session, tmp_path: Path, topics: tuple[Topic, Topic]
) -> None:
    first = build_digest(
        db, PERIOD_START, PERIOD_END, digests_dir=tmp_path, timezone="Europe/Moscow"
    )
    assert first.digest.status == DigestStatus.EMPTY

    _summarized(db, "2410.00001", "Late Arrival", [topics[0]])
    second = build_digest(
        db, PERIOD_START, PERIOD_END, digests_dir=tmp_path, timezone="Europe/Moscow"
    )

    assert second.digest.id == first.digest.id
    assert second.digest.status == DigestStatus.READY
    assert second.pdf_path == first.pdf_path
    assert "Late Arrival" in _pdf_text(second.pdf_path)
    assert len(db.scalars(select(Digest)).all()) == 1


def test_html_special_characters_in_titles_are_escaped(
    db: Session, tmp_path: Path, topics: tuple[Topic, Topic]
) -> None:
    _summarized(db, "2410.00001", "When <b>x</b> < y & z > 0: a study", [topics[0]])

    text = _pdf_text(_build(db, tmp_path))

    assert "When <b>x</b> < y & z > 0: a study" in text.replace("\n", " ")


# --- задача и API ---


def test_build_digest_task_passes_digest_id_to_next_step(
    db: Session, tmp_path: Path, topics: tuple[Topic, Topic], monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_logging("worker")
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    _summarized(db, "2410.00001", "Agents That Plan", [topics[0]])
    period = {"period_start": PERIOD_START.isoformat(), "period_end": PERIOD_END.isoformat()}
    run = runs.create_run(db, task_type=TaskType.BUILD_DIGEST, trigger=RunTrigger.MANUAL)
    db.commit()

    result = build_digest_task.apply(args=[period], kwargs={"run_id": run.id})

    assert result.successful()
    digest = db.scalar(select(Digest))
    assert digest is not None
    assert result.result == {**period, "digest_id": digest.id}
    db.refresh(run)
    assert run.status == RunStatus.SUCCESS
    assert run.stats["papers_count"] == 1
    assert run.stats["status"] == "ready"
    assert run.stats["pdf_size_bytes"] > 1000
    assert (tmp_path / "digests" / "digest_2024-10-05.pdf").is_file()


async def test_digests_api(
    auth_client: AsyncClient, db: Session, tmp_path: Path, topics: tuple[Topic, Topic]
) -> None:
    paper = _summarized(db, "2410.00001", "Agents That Plan", [topics[0]])
    built = build_digest(
        db, PERIOD_START, PERIOD_END, digests_dir=tmp_path, timezone="Europe/Moscow"
    )
    digest_id = built.digest.id

    listing = (await auth_client.get("/api/digests")).json()
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["status"] == "ready"
    assert item["papers_count"] == 1
    assert item["has_pdf"] is True
    assert item["filename"] == "digest_2024-10-05.pdf"
    assert "pdf_path" not in item

    assert (await auth_client.get("/api/digests", params={"status": "sent"})).json()["total"] == 0

    detail = (await auth_client.get(f"/api/digests/{digest_id}")).json()
    assert detail["papers"] == [
        {
            "position": 1,
            "paper_id": paper.id,
            "arxiv_id": "2410.00001",
            "title": "Agents That Plan",
            "abs_url": "https://arxiv.org/abs/2410.00001",
            "topic_id": topics[0].id,
            "topic_name": "Агенты",
        }
    ]
    assert detail["deliveries"] == []

    pdf = await auth_client.get(f"/api/digests/{digest_id}/pdf")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert "digest_2024-10-05.pdf" in pdf.headers["content-disposition"]
    assert pdf.content.startswith(b"%PDF-")

    built.pdf_path.unlink()
    missing = await auth_client.get(f"/api/digests/{digest_id}/pdf")
    assert missing.status_code == 404
    assert (await auth_client.get(f"/api/digests/{digest_id}")).json()["has_pdf"] is False
    assert (await auth_client.get("/api/digests/999999")).status_code == 404
