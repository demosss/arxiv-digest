from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Paper, PaperStatus, PaperTopic, Topic
from app.services.collector import Collector
from tests.factories import PERIOD_END, PERIOD_START, FakeSource, fake_extractor, make_entry


def _topic(db: Session, name: str, keywords: list[str], is_active: bool = True) -> Topic:
    queries = [f'all:"{keyword}"' for keyword in keywords]
    topic = Topic(name=name, queries=queries, arxiv_categories=["cs.CL"], is_active=is_active)
    db.add(topic)
    db.commit()
    return topic


def _collector(
    db: Session, source: FakeSource, tmp_path: Path, real_text: bool = False
) -> Collector:
    kwargs = {} if real_text else {"text_extractor": fake_extractor}
    return Collector(
        db, source, papers_dir=tmp_path / "papers", max_results_per_topic=200, **kwargs
    )


def test_collect_saves_papers_downloads_pdf_and_extracts_text(db: Session, tmp_path: Path) -> None:
    _topic(db, "LLM", ["large language model"])
    source = FakeSource(
        {"large language model": [make_entry("2410.00001"), make_entry("2410.00002")]}
    )

    stats = _collector(db, source, tmp_path, real_text=True).run(PERIOD_START, PERIOD_END)

    papers = db.scalars(select(Paper).order_by(Paper.arxiv_id)).all()
    assert [p.arxiv_id for p in papers] == ["2410.00001", "2410.00002"]
    for paper in papers:
        assert paper.status == PaperStatus.TEXT_EXTRACTED
        assert paper.pdf_path and Path(paper.pdf_path).is_file()
        assert paper.full_text and "real body text" in paper.full_text
        assert paper.text_chars == len(paper.full_text)
        assert paper.authors == ["Alice Smith", "Bob Jones"]
        assert paper.abs_url == f"https://arxiv.org/abs/{paper.arxiv_id}"
    assert (tmp_path / "papers" / "2410.00001.pdf").is_file()

    assert "submittedDate:[202410050000 TO 202410052359]" in source.queries[0]
    assert stats.as_dict() == {
        "period_start": PERIOD_START.isoformat(),
        "period_end": PERIOD_END.isoformat(),
        "topics_processed": 1,
        "papers_found": 2,
        "papers_new": 2,
        "duplicates": 0,
        "download_errors": 0,
        "text_extracted": 2,
        "topics": {"LLM": {"found": 2, "new": 2, "duplicates": 0, "download_errors": 0}},
    }


def test_paper_matched_by_several_topics_is_stored_once(db: Session, tmp_path: Path) -> None:
    llm = _topic(db, "LLM", ["large language model"])
    agents = _topic(db, "Агенты", ["agents", "tool use"])
    shared = make_entry(
        "2410.00001", title="LLM agents", abstract="Large language model agents with tool use."
    )
    source = FakeSource(
        {"large language model": [shared, make_entry("2410.00002")], "agents": [shared]}
    )

    stats = _collector(db, source, tmp_path).run(PERIOD_START, PERIOD_END)

    assert db.scalar(select(func.count()).select_from(Paper)) == 2
    paper = db.scalar(select(Paper).where(Paper.arxiv_id == "2410.00001"))
    assert paper is not None
    assert {link.topic_id for link in paper.topic_links} == {llm.id, agents.id}
    # PDF общей статьи скачан один раз
    assert source.downloads.count(shared.pdf_url) == 1

    assert stats.papers_found == 2
    assert stats.papers_new == 2
    assert stats.duplicates == 1
    assert stats.topics["LLM"].as_dict() == {
        "found": 2,
        "new": 2,
        "duplicates": 0,
        "download_errors": 0,
    }
    assert stats.topics["Агенты"].as_dict() == {
        "found": 1,
        "new": 0,
        "duplicates": 1,
        "download_errors": 0,
    }


def test_rerun_is_idempotent(db: Session, tmp_path: Path) -> None:
    _topic(db, "LLM", ["large language model"])
    source = FakeSource({"large language model": [make_entry("2410.00001")]})

    _collector(db, source, tmp_path).run(PERIOD_START, PERIOD_END)
    second = _collector(db, source, tmp_path).run(PERIOD_START, PERIOD_END)

    assert db.scalar(select(func.count()).select_from(Paper)) == 1
    assert db.scalar(select(func.count()).select_from(PaperTopic)) == 1
    assert len(source.downloads) == 1
    assert second.papers_found == 1
    assert second.papers_new == 0
    assert second.duplicates == 1
    assert second.text_extracted == 0


def test_download_error_marks_paper_failed_and_does_not_stop_others(
    db: Session, tmp_path: Path
) -> None:
    _topic(db, "LLM", ["large language model"])
    source = FakeSource(
        {"large language model": [make_entry("2410.00001"), make_entry("2410.00002")]},
        failing_ids={"2410.00001"},
    )

    stats = _collector(db, source, tmp_path).run(PERIOD_START, PERIOD_END)

    failed = db.scalar(select(Paper).where(Paper.arxiv_id == "2410.00001"))
    ok = db.scalar(select(Paper).where(Paper.arxiv_id == "2410.00002"))
    assert failed is not None and ok is not None
    assert failed.status == PaperStatus.FAILED
    assert failed.error and "HTTP 404" in failed.error
    assert ok.status == PaperStatus.TEXT_EXTRACTED
    assert stats.download_errors == 1
    assert stats.text_extracted == 1
    assert stats.topics["LLM"].download_errors == 1


def test_failed_download_is_retried_on_next_run(db: Session, tmp_path: Path) -> None:
    _topic(db, "LLM", ["large language model"])
    source = FakeSource(
        {"large language model": [make_entry("2410.00001")]}, failing_ids={"2410.00001"}
    )
    _collector(db, source, tmp_path).run(PERIOD_START, PERIOD_END)

    source.failing_ids.clear()
    stats = _collector(db, source, tmp_path).run(PERIOD_START, PERIOD_END)

    paper = db.scalar(select(Paper).where(Paper.arxiv_id == "2410.00001"))
    assert paper is not None
    db.refresh(paper)
    assert paper.status == PaperStatus.TEXT_EXTRACTED
    assert paper.error is None
    assert stats.text_extracted == 1


def test_empty_period_is_success_with_zero_stats(db: Session, tmp_path: Path) -> None:
    _topic(db, "LLM", ["large language model"])

    stats = _collector(db, FakeSource(), tmp_path).run(PERIOD_START, PERIOD_END)

    assert stats.papers_found == 0
    assert stats.as_dict()["topics"] == {
        "LLM": {"found": 0, "new": 0, "duplicates": 0, "download_errors": 0}
    }
    assert db.scalar(select(func.count()).select_from(Paper)) == 0


def test_inactive_topics_are_skipped(db: Session, tmp_path: Path) -> None:
    _topic(db, "LLM", ["large language model"], is_active=False)
    source = FakeSource({"large language model": [make_entry("2410.00001")]})

    stats = _collector(db, source, tmp_path).run(PERIOD_START, PERIOD_END)

    assert source.queries == []
    assert stats.papers_found == 0
    assert stats.topics == {}
