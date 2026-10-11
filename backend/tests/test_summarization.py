from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import health as health_api
from app.core.logging import configure_logging
from app.models import (
    Paper,
    PaperStatus,
    PaperTopic,
    Relevance,
    RunStatus,
    RunTrigger,
    Summary,
    TaskLog,
    TaskType,
    Topic,
)
from app.schemas.papers import LLMHealthOut
from app.services import runs
from app.services.llm_client import LLMClient, LLMHealth, LLMUnavailableError
from app.services.llm_summarizer import Summarizer
from app.services.relevance import RelevanceJudge
from app.services.summarization import fill_abstracts, summarize_papers
from app.tasks.pipeline import summarize_task
from tests.factories import PARAGRAPH, RUSSIAN_ABSTRACT, RUSSIAN_SUMMARY, FakeLLM, make_entry

UNAVAILABLE = LLMUnavailableError("LLM недоступна по адресу http://llm.test/v1: нет соединения")


def make_paper(
    db: Session,
    arxiv_id: str,
    status: PaperStatus = PaperStatus.TEXT_EXTRACTED,
    text: str | None = PARAGRAPH,
    title: str = "",
) -> Paper:
    entry = make_entry(arxiv_id, title=title)
    paper = Paper(
        arxiv_id=entry.arxiv_id,
        version=1,
        title=entry.title,
        authors=entry.authors,
        abstract=entry.abstract,
        categories=entry.categories,
        published_at=entry.published_at,
        pdf_url=entry.pdf_url,
        abs_url=entry.abs_url,
        status=status,
        full_text=text,
        text_chars=len(text) if text else None,
    )
    db.add(paper)
    db.commit()
    return paper


def _summarize(db: Session, llm: FakeLLM, **kwargs: Any) -> Any:
    return summarize_papers(db, Summarizer(llm, context_chars=24000), llm, **kwargs)


class DownLLM(FakeLLM):
    def health(self) -> LLMHealth:
        return LLMHealth(
            ok=False, base_url="http://llm.test/v1", model=self.model, error="нет соединения"
        )


def test_papers_get_russian_summaries(db: Session) -> None:
    first = make_paper(db, "2410.00001")
    second = make_paper(db, "2410.00002")
    skipped = make_paper(db, "2410.00003", status=PaperStatus.NEW, text=None)
    llm = FakeLLM()

    stats = _summarize(db, llm)

    assert stats.as_dict() == {
        "papers_total": 2,
        "summarized": 2,
        "failed": 0,
        "irrelevant": 0,
        "relevance_checks": 0,
        "llm_calls": 4,
        "chunks": 2,
        "prompt_tokens": 400,
        "completion_tokens": 160,
        "llm_duration_ms": 1000,
    }
    for paper in (first, second):
        db.refresh(paper)
        assert paper.status == PaperStatus.SUMMARIZED
        summary = db.scalar(select(Summary).where(Summary.paper_id == paper.id))
        assert summary is not None
        assert summary.text_ru == RUSSIAN_SUMMARY
        assert summary.abstract_ru == RUSSIAN_ABSTRACT
        assert summary.model == "fake-model"
        assert (summary.prompt_tokens, summary.completion_tokens) == (200, 80)
        assert summary.duration_ms == 500
        assert summary.chunks_count == 1
    db.refresh(skipped)
    assert skipped.status == PaperStatus.NEW


def test_failed_paper_does_not_stop_the_rest(db: Session) -> None:
    bad = make_paper(db, "2410.00001")
    good = make_paper(db, "2410.00002")
    llm = FakeLLM(["", ""])

    stats = _summarize(db, llm)

    db.refresh(bad)
    db.refresh(good)
    assert bad.status == PaperStatus.FAILED
    assert bad.error == "Модель дважды вернула некорректный ответ: ответ пустой"
    assert good.status == PaperStatus.SUMMARIZED
    assert (stats.summarized, stats.failed) == (1, 1)


def test_unavailable_llm_fails_all_papers_with_clear_error(db: Session) -> None:
    papers = [make_paper(db, f"2410.0000{i}") for i in range(1, 4)]
    llm = DownLLM()

    with pytest.raises(LLMUnavailableError) as exc_info:
        _summarize(db, llm)

    message = str(exc_info.value)
    assert "LLM недоступна по адресу http://llm.test/v1" in message
    assert "нет соединения" in message
    assert llm.calls == []
    for paper in papers:
        db.refresh(paper)
        assert paper.status == PaperStatus.FAILED
        assert paper.error == message


def test_llm_going_down_mid_run_aborts_after_three_errors(db: Session) -> None:
    papers = [make_paper(db, f"2410.0000{i}") for i in range(1, 7)]
    llm = FakeLLM([RUSSIAN_ABSTRACT, RUSSIAN_SUMMARY, UNAVAILABLE, UNAVAILABLE, UNAVAILABLE])

    with pytest.raises(LLMUnavailableError):
        _summarize(db, llm)

    for paper in papers:
        db.refresh(paper)
    assert [p.status for p in papers] == [PaperStatus.SUMMARIZED] + [PaperStatus.FAILED] * 5
    # до пятой и шестой статей дело не дошло
    assert len(llm.calls) == 5
    assert all(p.error and "LLM недоступна" in p.error for p in papers[1:])


def test_explicit_paper_ids_replace_existing_summary(db: Session) -> None:
    paper = make_paper(db, "2410.00001", status=PaperStatus.SUMMARIZED)
    other = make_paper(db, "2410.00002")
    db.add(Summary(paper_id=paper.id, text_ru="Старое резюме", model="old-model"))
    db.commit()

    stats = _summarize(db, FakeLLM(), paper_ids=[paper.id])

    assert stats.papers_total == 1
    summaries = db.scalars(select(Summary).where(Summary.paper_id == paper.id)).all()
    assert len(summaries) == 1
    db.refresh(summaries[0])
    assert summaries[0].text_ru == RUSSIAN_SUMMARY
    assert summaries[0].model == "fake-model"
    db.refresh(other)
    assert other.status == PaperStatus.TEXT_EXTRACTED


def test_retry_failed_picks_up_failed_papers_with_text(db: Session) -> None:
    failed = make_paper(db, "2410.00001", status=PaperStatus.FAILED)
    no_text = make_paper(db, "2410.00002", status=PaperStatus.FAILED, text=None)

    assert _summarize(db, FakeLLM()).papers_total == 0

    stats = _summarize(db, FakeLLM(), retry_failed=True)

    assert stats.summarized == 1
    db.refresh(failed)
    db.refresh(no_text)
    assert failed.status == PaperStatus.SUMMARIZED
    assert failed.error is None
    assert no_text.status == PaperStatus.FAILED


# --- дописывание аннотаций ---


def test_fill_abstracts_touches_only_summaries_without_abstract(db: Session) -> None:
    old = make_paper(db, "2410.00001", PaperStatus.SUMMARIZED)
    done = make_paper(db, "2410.00002", PaperStatus.SUMMARIZED)
    waiting = make_paper(db, "2410.00003")
    db.add_all(
        [
            Summary(paper_id=old.id, text_ru="Старое резюме", model="old-model"),
            Summary(paper_id=done.id, text_ru="Резюме", abstract_ru="Уже есть", model="old-model"),
        ]
    )
    db.commit()
    llm = FakeLLM()

    stats = fill_abstracts(db, Summarizer(llm, context_chars=24000), llm)

    assert (stats.papers_total, stats.summarized, stats.failed, stats.llm_calls) == (1, 1, 0, 1)
    assert [call["purpose"] for call in llm.calls] == ["abstract"]
    summary = db.scalar(select(Summary).where(Summary.paper_id == old.id))
    assert summary is not None
    db.refresh(summary)
    # резюме и статус статьи не тронуты
    assert (summary.abstract_ru, summary.text_ru, summary.model) == (
        RUSSIAN_ABSTRACT,
        "Старое резюме",
        "old-model",
    )
    kept = db.scalar(select(Summary).where(Summary.paper_id == done.id))
    assert kept is not None and kept.abstract_ru == "Уже есть"
    db.refresh(old)
    db.refresh(waiting)
    assert (old.status, waiting.status) == (PaperStatus.SUMMARIZED, PaperStatus.TEXT_EXTRACTED)


def test_fill_abstracts_keeps_the_paper_when_the_answer_is_invalid(db: Session) -> None:
    bad = make_paper(db, "2410.00001", PaperStatus.SUMMARIZED)
    good = make_paper(db, "2410.00002", PaperStatus.SUMMARIZED)
    db.add_all(
        [
            Summary(paper_id=bad.id, text_ru="Резюме", model="old-model"),
            Summary(paper_id=good.id, text_ru="Резюме", model="old-model"),
        ]
    )
    db.commit()
    llm = FakeLLM(["", ""])

    stats = fill_abstracts(db, Summarizer(llm, context_chars=24000), llm)

    assert (stats.summarized, stats.failed) == (1, 1)
    db.refresh(bad)
    assert (bad.status, bad.error) == (PaperStatus.SUMMARIZED, None)
    assert db.scalar(select(Summary.abstract_ru).where(Summary.paper_id == bad.id)) is None


def test_fill_abstracts_needs_the_llm(db: Session) -> None:
    paper = make_paper(db, "2410.00001", PaperStatus.SUMMARIZED)
    db.add(Summary(paper_id=paper.id, text_ru="Резюме", model="old-model"))
    db.commit()
    llm = DownLLM()

    with pytest.raises(LLMUnavailableError, match="LLM недоступна"):
        fill_abstracts(db, Summarizer(llm, context_chars=24000), llm)

    db.refresh(paper)
    assert paper.status == PaperStatus.SUMMARIZED


# --- проверка релевантности ---

YES = '{"relevance": "yes", "reason": "Статья по теме."}'
PARTIAL = '{"relevance": "partial", "reason": "Смежная область."}'
NO = '{"relevance": "no", "reason": "Совпадение слов."}'


def _link(db: Session, paper: Paper, *names: str) -> list[PaperTopic]:
    links = []
    for name in names:
        topic = Topic(name=name, queries=[f'all:"{name}"'], description=f"Описание {name}")
        db.add(topic)
        db.flush()
        links.append(PaperTopic(paper_id=paper.id, topic_id=topic.id, matched_queries=[]))
    db.add_all(links)
    db.commit()
    return links


def _summarize_with_judge(db: Session, llm: FakeLLM, **kwargs: Any) -> Any:
    return _summarize(db, llm, judge=RelevanceJudge(llm), **kwargs)


def test_irrelevant_paper_is_dropped_without_summary(db: Session, tmp_path: Path) -> None:
    paper = make_paper(db, "2410.00001")
    pdf = tmp_path / "2410.00001.pdf"
    pdf.write_bytes(b"%PDF")
    paper.pdf_path = str(pdf)
    (link,) = _link(db, paper, "ODD")
    kept = make_paper(db, "2410.00002")
    _link(db, kept, "Суда")
    llm = FakeLLM([NO, PARTIAL])

    stats = _summarize_with_judge(db, llm)

    assert (stats.irrelevant, stats.summarized, stats.failed) == (1, 1, 0)
    assert stats.relevance_checks == 2
    db.refresh(paper)
    assert paper.status == PaperStatus.IRRELEVANT
    assert (paper.pdf_path, paper.text_chars, paper.error) == (None, None, None)
    assert db.scalar(select(Paper.full_text).where(Paper.id == paper.id)) is None
    assert not pdf.exists()
    assert db.scalar(select(Summary).where(Summary.paper_id == paper.id)) is None
    db.refresh(link)
    assert (link.relevance, link.relevance_reason) == (Relevance.IRRELEVANT, "Совпадение слов.")
    db.refresh(kept)
    assert kept.status == PaperStatus.SUMMARIZED
    assert kept.topic_links[0].relevance == Relevance.PARTIAL
    # нерелевантная статья до суммаризации не доходит
    assert [call["purpose"] for call in llm.calls] == [
        "relevance",
        "relevance",
        "abstract",
        "summary",
    ]


def test_paper_relevant_to_one_of_its_topics_is_summarized(db: Session) -> None:
    paper = make_paper(db, "2410.00001")
    first, second = _link(db, paper, "ODD", "Суда")

    stats = _summarize_with_judge(db, FakeLLM([NO, YES]))

    assert (stats.irrelevant, stats.summarized) == (0, 1)
    db.refresh(paper)
    assert paper.status == PaperStatus.SUMMARIZED
    assert {first.relevance, second.relevance} == {Relevance.IRRELEVANT, Relevance.RELEVANT}


def test_answer_out_of_format_does_not_drop_the_paper(db: Session) -> None:
    paper = make_paper(db, "2410.00001")
    (link,) = _link(db, paper, "ODD")

    stats = _summarize_with_judge(db, FakeLLM(["не знаю", "не уверен"]))

    assert (stats.irrelevant, stats.summarized, stats.relevance_checks) == (0, 1, 1)
    db.refresh(link)
    assert link.relevance is None


def test_existing_verdicts_are_not_asked_again(db: Session) -> None:
    paper = make_paper(db, "2410.00001")
    (link,) = _link(db, paper, "ODD")
    link.relevance = Relevance.RELEVANT
    db.commit()
    llm = FakeLLM()

    stats = _summarize_with_judge(db, llm)

    assert (stats.relevance_checks, stats.summarized) == (0, 1)
    assert [call["purpose"] for call in llm.calls] == ["abstract", "summary"]


def test_explicitly_chosen_papers_are_not_checked(db: Session) -> None:
    paper = make_paper(db, "2410.00001")
    _link(db, paper, "ODD")
    llm = FakeLLM()

    stats = _summarize_with_judge(db, llm, paper_ids=[paper.id])

    assert (stats.relevance_checks, stats.summarized) == (0, 1)


def test_llm_error_during_relevance_check_fails_the_paper(db: Session) -> None:
    paper = make_paper(db, "2410.00001")
    (link,) = _link(db, paper, "ODD")

    stats = _summarize_with_judge(db, FakeLLM([UNAVAILABLE]))

    assert (stats.failed, stats.irrelevant, stats.summarized) == (1, 0, 0)
    db.refresh(paper)
    assert paper.status == PaperStatus.FAILED
    db.refresh(link)
    assert link.relevance is None


def test_nothing_to_do_does_not_touch_llm(db: Session) -> None:
    llm = DownLLM()

    assert _summarize(db, llm).as_dict()["papers_total"] == 0


# --- задача Celery ---


def test_summarize_task_fails_run_when_llm_is_down(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_logging("worker-llm")
    paper = make_paper(db, "2410.00001")
    monkeypatch.setattr(LLMClient, "from_settings", classmethod(lambda cls, settings: DownLLM()))
    run = runs.create_run(db, task_type=TaskType.SUMMARIZE, trigger=RunTrigger.MANUAL)
    db.commit()

    result = summarize_task.apply(args=[{}], kwargs={"run_id": run.id})

    assert result.failed()
    db.refresh(run)
    db.refresh(paper)
    assert run.status == RunStatus.FAILED
    assert run.error and run.error.startswith("LLM недоступна по адресу http://llm.test/v1")
    assert paper.status == PaperStatus.FAILED
    messages = db.scalars(select(TaskLog.message).where(TaskLog.run_id == run.id)).all()
    assert "summarize_llm_unavailable" in messages
    assert "step_failed" in messages


def test_summarize_task_success(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    configure_logging("worker-llm")
    paper = make_paper(db, "2410.00001")
    monkeypatch.setattr(LLMClient, "from_settings", classmethod(lambda cls, settings: FakeLLM()))
    run = runs.create_run(db, task_type=TaskType.SUMMARIZE, trigger=RunTrigger.MANUAL)
    db.commit()

    result = summarize_task.apply(args=[{"paper_ids": [paper.id]}], kwargs={"run_id": run.id})

    assert result.successful()
    db.refresh(run)
    assert run.status == RunStatus.SUCCESS
    assert run.stats["summarized"] == 1
    logs = db.scalars(select(TaskLog).where(TaskLog.run_id == run.id)).all()
    summarized = next(entry for entry in logs if entry.message == "paper_summarized")
    assert summarized.extra["arxiv_id"] == "2410.00001"
    assert summarized.extra["prompt_tokens"] == 200


def test_summarize_task_can_only_fill_abstracts(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_logging("worker-llm")
    old = make_paper(db, "2410.00001", PaperStatus.SUMMARIZED)
    db.add(Summary(paper_id=old.id, text_ru="Старое резюме", model="old-model"))
    waiting = make_paper(db, "2410.00002")
    monkeypatch.setattr(LLMClient, "from_settings", classmethod(lambda cls, settings: FakeLLM()))
    run = runs.create_run(db, task_type=TaskType.SUMMARIZE, trigger=RunTrigger.MANUAL)
    db.commit()

    result = summarize_task.apply(args=[{"fill_abstracts": True}], kwargs={"run_id": run.id})

    assert result.successful()
    db.refresh(run)
    assert (run.stats["papers_total"], run.stats["summarized"]) == (1, 1)
    db.refresh(waiting)
    assert waiting.status == PaperStatus.TEXT_EXTRACTED


# --- API ---


async def test_papers_list_and_detail(auth_client: AsyncClient, db: Session) -> None:
    topic = Topic(name="LLM", queries=['all:"large language model"'])
    db.add(topic)
    db.commit()
    summarized = make_paper(db, "2410.00001", PaperStatus.SUMMARIZED, title="Agents with tools")
    extracted = make_paper(db, "2410.00002", title="Diffusion models")
    db.add_all(
        [
            PaperTopic(
                paper_id=summarized.id, topic_id=topic.id, matched_keywords=["large language model"]
            ),
            Summary(paper_id=summarized.id, text_ru=RUSSIAN_SUMMARY, model="fake-model"),
        ]
    )
    db.commit()

    listing = (await auth_client.get("/api/papers")).json()
    assert listing["total"] == 2
    by_id = {item["id"]: item for item in listing["items"]}
    assert by_id[summarized.id]["has_summary"] is True
    assert by_id[summarized.id]["topics"] == [
        {
            "id": topic.id,
            "name": "LLM",
            "matched_keywords": ["large language model"],
            "matched_queries": [],
            "relevance": None,
            "relevance_reason": None,
        }
    ]
    assert by_id[extracted.id]["has_summary"] is False
    assert "full_text" not in by_id[extracted.id]

    by_status = (await auth_client.get("/api/papers", params={"status": "summarized"})).json()
    assert [p["id"] for p in by_status["items"]] == [summarized.id]
    by_topic = (await auth_client.get("/api/papers", params={"topic_id": topic.id})).json()
    assert [p["id"] for p in by_topic["items"]] == [summarized.id]
    by_title = (await auth_client.get("/api/papers", params={"q": "diffusion"})).json()
    assert [p["id"] for p in by_title["items"]] == [extracted.id]
    by_date = (
        await auth_client.get("/api/papers", params={"date_from": "2024-10-06T00:00:00Z"})
    ).json()
    assert by_date["total"] == 0

    detail = (await auth_client.get(f"/api/papers/{summarized.id}")).json()
    assert detail["abstract"]
    assert detail["summary"]["text_ru"] == RUSSIAN_SUMMARY
    assert detail["summary"]["abstract_ru"] is None
    assert detail["summary"]["model"] == "fake-model"
    assert (await auth_client.get(f"/api/papers/{extracted.id}")).json()["summary"] is None
    assert (await auth_client.get("/api/papers/999999")).status_code == 404


async def test_resummarize_enqueues_run(
    auth_client: AsyncClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    published: list[dict[str, Any]] = []
    monkeypatch.setattr(
        summarize_task,
        "apply_async",
        lambda args=None, kwargs=None, **options: published.append({"args": args, **options}),
    )
    paper = make_paper(db, "2410.00001", PaperStatus.SUMMARIZED)
    no_text = make_paper(db, "2410.00002", PaperStatus.FAILED, text=None)

    response = await auth_client.post(f"/api/papers/{paper.id}/resummarize")

    assert response.status_code == 202
    run = response.json()
    assert run["task_type"] == "summarize"
    assert run["params"] == {"paper_ids": [paper.id]}
    assert published[0]["args"] == [{"paper_ids": [paper.id]}]

    conflict = await auth_client.post(f"/api/papers/{no_text.id}/resummarize")
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "no_text"
    assert len(published) == 1


async def test_llm_health_endpoint(
    auth_client: AsyncClient, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        health_api,
        "check_llm",
        lambda: LLMHealthOut(
            ok=False,
            base_url="http://llm.test/v1",
            model="qwen-test",
            latency_ms=None,
            model_available=None,
            models=[],
            error="нет соединения",
        ),
    )

    response = await auth_client.get("/api/llm/health")

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["error"] == "нет соединения"

    del auth_client.headers["Authorization"]
    assert (await client.get("/api/llm/health")).status_code == 401


async def test_delete_paper_removes_summary_and_pdf(
    auth_client: AsyncClient, db: Session, tmp_path: Path
) -> None:
    paper = make_paper(db, "2410.00001", PaperStatus.SUMMARIZED)
    pdf = tmp_path / "2410.00001.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    paper.pdf_path = str(pdf)
    db.add(Summary(paper_id=paper.id, text_ru=RUSSIAN_SUMMARY, model="fake-model"))
    db.commit()

    paper_id = paper.id
    assert (await auth_client.delete(f"/api/papers/{paper_id}")).status_code == 204

    db.expire_all()
    assert db.scalars(select(Paper)).all() == []
    assert db.scalars(select(Summary)).all() == []
    assert not pdf.exists()
    assert (await auth_client.delete(f"/api/papers/{paper_id}")).status_code == 404


async def test_clear_papers_respects_filters(auth_client: AsyncClient, db: Session) -> None:
    make_paper(db, "2410.00001", PaperStatus.SUMMARIZED)
    make_paper(db, "2410.00002", PaperStatus.FAILED, text=None)
    kept = make_paper(db, "2410.00003")

    by_status = await auth_client.delete("/api/papers", params={"status": "failed"})
    assert by_status.status_code == 200
    assert by_status.json() == {"deleted": 1}
    by_query = await auth_client.delete("/api/papers", params={"q": "2410.00001"})
    assert by_query.json() == {"deleted": 1}
    assert [p["id"] for p in (await auth_client.get("/api/papers")).json()["items"]] == [kept.id]

    assert (await auth_client.delete("/api/papers")).json() == {"deleted": 1}
    assert (await auth_client.get("/api/papers")).json()["total"] == 0


async def test_papers_are_not_deleted_while_runs_are_active(
    auth_client: AsyncClient, db: Session
) -> None:
    paper = make_paper(db, "2410.00001")
    run = runs.create_run(db, task_type=TaskType.SUMMARIZE, trigger=RunTrigger.MANUAL)
    db.commit()

    single = await auth_client.delete(f"/api/papers/{paper.id}")
    assert single.status_code == 409
    assert single.json()["code"] == "runs_active"
    assert (await auth_client.delete("/api/papers")).status_code == 409

    runs.finish_run(db, run, RunStatus.SUCCESS)
    assert (await auth_client.delete(f"/api/papers/{paper.id}")).status_code == 204
