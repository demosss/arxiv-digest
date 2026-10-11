from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import (
    Digest,
    Paper,
    PaperStatus,
    Profile,
    Recipient,
    RunStatus,
    RunTrigger,
    Schedule,
    Summary,
    TaskRun,
    TaskType,
    Topic,
)
from app.services import runs
from app.services.collector import Collector
from app.services.digest_builder import build_digest
from app.services.llm_summarizer import Summarizer
from app.services.mailer import run_send_digest
from app.services.pdf_text import ExtractedText
from app.services.scheduler import tick
from app.services.summarization import summarize_papers
from app.tasks import launcher
from tests.conftest import PROFILE_ID
from tests.factories import (
    PARAGRAPH,
    PERIOD_END,
    PERIOD_START,
    RUSSIAN_ABSTRACT,
    RUSSIAN_SUMMARY,
    FakeLLM,
    FakeSource,
    fake_extractor,
    make_entry,
)
from tests.test_mailer import FakeSMTP, _smtp_settings
from tests.test_scheduler import NOW, FakeLauncher

LLM_QUERY = 'all:"large language model"'
NO_DATA = {"topics": 0, "recipients": 0, "schedules": 0, "papers": 0, "digests": 0}


@pytest.fixture
def second(db: Session) -> Profile:
    profile = Profile(name="Беспилотники")
    db.add(profile)
    db.commit()
    return profile


def _headers(profile: Profile) -> dict[str, str]:
    return {"X-Profile-Id": str(profile.id)}


def _paper(
    db: Session,
    profile_id: int,
    arxiv_id: str = "2410.00001",
    status: PaperStatus = PaperStatus.TEXT_EXTRACTED,
    *,
    summary: bool = False,
) -> Paper:
    entry = make_entry(arxiv_id)
    paper = Paper(
        profile_id=profile_id,
        arxiv_id=entry.arxiv_id,
        version=entry.version,
        title=entry.title,
        authors=entry.authors,
        abstract=entry.abstract,
        categories=entry.categories,
        published_at=entry.published_at,
        pdf_url=entry.pdf_url,
        abs_url=entry.abs_url,
        status=status,
        full_text=PARAGRAPH,
        text_chars=len(PARAGRAPH),
    )
    db.add(paper)
    db.flush()
    if summary:
        db.add(
            Summary(
                paper_id=paper.id,
                text_ru=RUSSIAN_SUMMARY,
                abstract_ru=RUSSIAN_ABSTRACT,
                model="donor-model",
                chunks_count=3,
            )
        )
    db.commit()
    return paper


# --- администрирование профилей ---


async def test_profile_crud(auth_client: AsyncClient) -> None:
    listing = await auth_client.get("/api/profiles")
    assert [(p["id"], p["name"], p["counts"]) for p in listing.json()] == [
        (PROFILE_ID, "Основной", NO_DATA)
    ]

    created = await auth_client.post("/api/profiles", json={"name": "  Беспилотники "})
    assert created.status_code == 201
    profile = created.json()
    assert (profile["name"], profile["counts"]) == ("Беспилотники", NO_DATA)

    duplicate = await auth_client.post("/api/profiles", json={"name": "Основной"})
    assert (duplicate.status_code, duplicate.json()["code"]) == (409, "conflict")
    assert (await auth_client.post("/api/profiles", json={"name": "   "})).status_code == 422

    url = f"/api/profiles/{profile['id']}"
    renamed = await auth_client.patch(url, json={"name": "Морские суда"})
    assert (renamed.status_code, renamed.json()["name"]) == (200, "Морские суда")
    assert (await auth_client.patch(url, json={"name": "Основной"})).status_code == 409
    assert (await auth_client.patch("/api/profiles/999", json={"name": "X"})).status_code == 404
    assert [p["name"] for p in (await auth_client.get("/api/profiles")).json()] == [
        "Основной",
        "Морские суда",
    ]

    assert (await auth_client.delete(url)).status_code == 204
    assert (await auth_client.delete("/api/profiles/999")).status_code == 404
    last = await auth_client.delete(f"/api/profiles/{PROFILE_ID}")
    assert (last.status_code, last.json()["code"]) == (409, "last_profile")


async def test_only_empty_profile_can_be_deleted(
    auth_client: AsyncClient, db: Session, second: Profile
) -> None:
    headers = _headers(second)
    topic = await auth_client.post(
        "/api/topics", json={"name": "LLM", "queries": [LLM_QUERY]}, headers=headers
    )
    recipient = await auth_client.post(
        "/api/recipients", json={"email": "a@example.com"}, headers=headers
    )
    assert (topic.status_code, recipient.status_code) == (201, 201)
    run = runs.create_run(
        db, profile_id=second.id, task_type=TaskType.COLLECT, trigger=RunTrigger.MANUAL
    )
    db.commit()
    url = f"/api/profiles/{second.id}"

    counts = {p["id"]: p["counts"] for p in (await auth_client.get("/api/profiles")).json()}
    assert counts == {PROFILE_ID: NO_DATA, second.id: {**NO_DATA, "topics": 1, "recipients": 1}}

    refused = await auth_client.delete(url)
    assert (refused.status_code, refused.json()["code"]) == (409, "profile_not_empty")
    assert "темы — 1, получатели — 1" in refused.json()["detail"]

    await auth_client.delete(f"/api/topics/{topic.json()['id']}")
    await auth_client.delete(f"/api/recipients/{recipient.json()['id']}")
    # данных больше нет, но запуск профиля ещё в очереди
    active = await auth_client.delete(url)
    assert (active.status_code, active.json()["code"]) == (409, "runs_active")

    runs.finish_run(db, run, RunStatus.SUCCESS)
    assert (await auth_client.delete(url)).status_code == 204
    # история запусков удаляется вместе с профилем
    assert db.scalar(select(func.count()).select_from(TaskRun)) == 0
    assert [p["id"] for p in (await auth_client.get("/api/profiles")).json()] == [PROFILE_ID]


# --- данные профилей не пересекаются ---


async def test_settings_are_separate_for_each_profile(
    auth_client: AsyncClient, second: Profile
) -> None:
    headers = _headers(second)
    schedule = {"name": "Утро", "task_type": "full_pipeline", "cron_expression": "0 6 * * *"}
    # одинаковые название темы и адрес получателя в разных профилях не конфликтуют
    for extra in ({}, {"headers": headers}):
        topic = await auth_client.post(
            "/api/topics", json={"name": "LLM", "queries": [LLM_QUERY]}, **extra
        )
        recipient = await auth_client.post(
            "/api/recipients", json={"email": "a@example.com"}, **extra
        )
        assert (topic.status_code, recipient.status_code) == (201, 201)
    assert (await auth_client.post("/api/schedules", json=schedule, headers=headers)).is_success
    await auth_client.post("/api/topics", json={"name": "Агенты", "queries": ["all:agents"]})

    # без заголовка запрос работает с первым профилем
    names = [t["name"] for t in (await auth_client.get("/api/topics")).json()["items"]]
    assert names == ["LLM", "Агенты"]
    names = [
        t["name"] for t in (await auth_client.get("/api/topics", headers=headers)).json()["items"]
    ]
    assert names == ["LLM"]
    assert (await auth_client.get("/api/recipients", headers=headers)).json()["total"] == 1
    assert (await auth_client.get("/api/schedules")).json()["total"] == 0
    assert (await auth_client.get("/api/schedules", headers=headers)).json()["total"] == 1

    duplicate = await auth_client.post(
        "/api/topics", json={"name": "LLM", "queries": [LLM_QUERY]}, headers=headers
    )
    assert duplicate.status_code == 409

    exported = await auth_client.get("/api/topics/export", headers=headers)
    assert "LLM" in exported.text and "Агенты" not in exported.text

    unknown = await auth_client.get("/api/topics", headers={"X-Profile-Id": "999"})
    assert (unknown.status_code, unknown.json()["code"]) == (404, "profile_not_found")


async def test_papers_digests_and_runs_are_listed_per_profile(
    auth_client: AsyncClient, db: Session, second: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    published: list[int] = []
    monkeypatch.setattr(
        launcher.collect_task,
        "apply_async",
        lambda args=None, kwargs=None, **options: published.append(kwargs["run_id"]),
    )
    headers = _headers(second)
    _paper(db, PROFILE_ID, status=PaperStatus.SUMMARIZED)
    other = _paper(db, second.id, status=PaperStatus.SUMMARIZED)
    db.add(Digest(profile_id=second.id, period_start=PERIOD_START, period_end=PERIOD_END))
    db.commit()

    launched = await auth_client.post("/api/runs", json={"task_type": "collect"}, headers=headers)
    assert launched.status_code == 202
    assert db.get_one(TaskRun, launched.json()["id"]).profile_id == second.id
    assert published == [launched.json()["id"]]

    for url, mine, theirs in (
        ("/api/papers", 1, 1),
        ("/api/digests", 0, 1),
        ("/api/runs", 0, 1),
    ):
        assert (await auth_client.get(url)).json()["total"] == mine, url
        assert (await auth_client.get(url, headers=headers)).json()["total"] == theirs, url
    board = (await auth_client.get("/api/dashboard")).json()
    assert (board["counters"]["papers_collected"], board["recent_runs"]) == (1, [])
    board = (await auth_client.get("/api/dashboard", headers=headers)).json()
    assert [run["id"] for run in board["recent_runs"]] == [launched.json()["id"]]

    # очистка затрагивает только свой профиль, и чужой активный запуск ей не мешает
    cleared = await auth_client.delete("/api/papers")
    assert (cleared.status_code, cleared.json()) == (200, {"deleted": 1})
    assert db.scalars(select(Paper.id)).all() == [other.id]
    busy = await auth_client.delete("/api/papers", headers=headers)
    assert (busy.status_code, busy.json()["code"]) == (409, "runs_active")


async def test_paper_is_sent_only_to_recipients_of_its_profile(
    auth_client: AsyncClient, db: Session, second: Profile, tmp_path: Path
) -> None:
    paper = _paper(db, PROFILE_ID)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    paper.pdf_path = str(pdf)
    stranger = Recipient(profile_id=second.id, email="b@example.com")
    db.add(stranger)
    db.commit()

    response = await auth_client.post(
        f"/api/papers/{paper.id}/send", json={"recipient_ids": [stranger.id], "include_pdf": True}
    )

    assert (response.status_code, response.json()["code"]) == (404, "not_found")


# --- одна статья в двух профилях ---


def _collector(
    db: Session, source: FakeSource, profile_id: int, tmp_path: Path, **kwargs: Any
) -> Any:
    kwargs.setdefault("text_extractor", fake_extractor)
    return Collector(
        db,
        source,
        profile_id=profile_id,
        papers_dir=tmp_path / "papers",
        max_results_per_topic=200,
        **kwargs,
    )


def test_collect_copies_pdf_and_text_from_another_profile(
    db: Session, second: Profile, tmp_path: Path
) -> None:
    for profile_id in (PROFILE_ID, second.id):
        db.add(Topic(profile_id=profile_id, name="LLM", queries=[LLM_QUERY]))
    db.commit()
    results = {"large language model": [make_entry("2410.00001")]}
    _collector(db, FakeSource(results), PROFILE_ID, tmp_path).run(PERIOD_START, PERIOD_END)

    def no_extraction(path: Path) -> ExtractedText:
        raise AssertionError("текст статьи должен быть взят из другого профиля")

    source = FakeSource(results)
    stats = _collector(db, source, second.id, tmp_path, text_extractor=no_extraction).run(
        PERIOD_START, PERIOD_END
    )

    assert (stats.papers_new, stats.text_extracted, stats.text_reused) == (1, 1, 1)
    assert source.downloads == []
    first, copy = db.scalars(select(Paper).order_by(Paper.id)).all()
    db.refresh(copy, ["full_text"])
    db.refresh(first, ["full_text"])
    # запись у каждого профиля своя, со своим файлом: удаление одной не задевает другую
    assert (first.profile_id, copy.profile_id) == (PROFILE_ID, second.id)
    assert copy.status == PaperStatus.TEXT_EXTRACTED
    assert copy.full_text == first.full_text and copy.text_chars == first.text_chars
    assert copy.pdf_path == str(tmp_path / "papers" / str(second.id) / "2410.00001.pdf")
    assert copy.pdf_path != first.pdf_path
    assert Path(copy.pdf_path).read_bytes() == Path(first.pdf_path or "").read_bytes()
    # повторный сбор в первом профиле не считает чужую запись своим дубликатом
    assert [link.topic.profile_id for link in copy.topic_links] == [second.id]


def test_summary_is_copied_from_another_profile(db: Session, second: Profile) -> None:
    _paper(db, PROFILE_ID, status=PaperStatus.SUMMARIZED, summary=True)
    paper = _paper(db, second.id)
    llm = FakeLLM()
    summarizer = Summarizer(llm, context_chars=24000)

    stats = summarize_papers(db, summarizer, llm, profile_id=second.id)

    assert (stats.papers_total, stats.reused, stats.summarized) == (1, 1, 0)
    assert llm.calls == []
    db.refresh(paper)
    assert paper.status == PaperStatus.SUMMARIZED
    assert paper.summary is not None
    assert (paper.summary.text_ru, paper.summary.abstract_ru) == (RUSSIAN_SUMMARY, RUSSIAN_ABSTRACT)
    assert (paper.summary.model, paper.summary.chunks_count) == ("donor-model", 3)

    # «пересуммаризировать» — явная просьба: резюме пишется заново, а не копируется
    stats = summarize_papers(db, summarizer, llm, profile_id=second.id, paper_ids=[paper.id])

    assert (stats.reused, stats.summarized) == (0, 1)
    assert llm.calls
    db.refresh(paper.summary)
    assert paper.summary.model == "fake-model"


def test_summarize_takes_only_papers_of_its_profile(db: Session, second: Profile) -> None:
    mine = _paper(db, PROFILE_ID, "2410.00001")
    theirs = _paper(db, second.id, "2410.00002")
    llm = FakeLLM()

    stats = summarize_papers(db, Summarizer(llm, context_chars=24000), llm, profile_id=PROFILE_ID)

    assert stats.summarized == 1
    db.refresh(mine)
    db.refresh(theirs)
    assert (mine.status, theirs.status) == (PaperStatus.SUMMARIZED, PaperStatus.TEXT_EXTRACTED)


# --- запуски и расписания ---


def test_pipelines_of_different_profiles_do_not_block_each_other(
    db: Session, second: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher, "publish_pipeline", lambda steps, params: None)

    def launch(profile_id: int) -> TaskRun:
        return launcher.launch_run(
            db,
            profile_id=profile_id,
            task_type=TaskType.FULL_PIPELINE,
            trigger=RunTrigger.SCHEDULE,
        )

    first, other, repeated = launch(PROFILE_ID), launch(second.id), launch(PROFILE_ID)

    assert (first.status, other.status) == (RunStatus.QUEUED, RunStatus.QUEUED)
    assert {step.profile_id for step in other.children} == {second.id}
    assert repeated.status == RunStatus.SKIPPED
    assert f"#{first.id}" in (repeated.error or "")


def test_schedule_launches_run_in_its_profile(db: Session, second: Profile) -> None:
    db.add(
        Schedule(
            profile_id=second.id,
            name="Утро",
            task_type=TaskType.FULL_PIPELINE,
            cron_expression="0 9 * * *",
            timezone="Europe/Moscow",
            next_fire_at=NOW.replace(second=0),
        )
    )
    db.commit()
    launch = FakeLauncher()

    (run,) = tick(db, launch, NOW)

    assert launch.calls[0]["profile_id"] == second.id
    assert run.profile_id == second.id


# --- дайджест и рассылка ---


def test_digest_is_built_and_sent_per_profile(db: Session, second: Profile, tmp_path: Path) -> None:
    _smtp_settings(db)
    _paper(db, PROFILE_ID, "2410.00001", PaperStatus.SUMMARIZED, summary=True)
    _paper(db, second.id, "2410.00002", PaperStatus.SUMMARIZED, summary=True)
    _paper(db, second.id, "2410.00003", PaperStatus.SUMMARIZED, summary=True)
    db.add_all(
        [
            Recipient(profile_id=PROFILE_ID, email="a@example.com"),
            Recipient(profile_id=second.id, email="b@example.com"),
        ]
    )
    db.commit()

    def build(profile_id: int) -> Digest:
        return build_digest(
            db,
            PERIOD_START,
            PERIOD_END,
            profile_id=profile_id,
            digests_dir=tmp_path / "digests",
            timezone="Europe/Moscow",
        ).digest

    mine, theirs = build(PROFILE_ID), build(second.id)

    # один и тот же период — два дайджеста, каждый со своими статьями и своим файлом
    assert (mine.papers_count, theirs.papers_count) == (1, 2)
    assert mine.pdf_path == str(tmp_path / "digests" / str(PROFILE_ID) / "digest_2024-10-05.pdf")
    assert theirs.pdf_path == str(tmp_path / "digests" / str(second.id) / "digest_2024-10-05.pdf")
    assert build(second.id).id == theirs.id

    smtp = FakeSMTP()
    outcome = run_send_digest(
        db,
        get_settings(),
        profile_id=second.id,
        smtp_factory=smtp,
        sleep=lambda _: None,  # type: ignore[arg-type]
    )

    assert outcome.digest_id == theirs.id
    assert smtp.recipients == ["b@example.com"]
    sent_on = datetime.now(ZoneInfo(get_settings().tz)).strftime("%d.%m.%Y")
    assert smtp.sent[0]["Subject"] == f"arXiv Digest - Беспилотники - {sent_on}"
