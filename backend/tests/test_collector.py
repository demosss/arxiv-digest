from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Paper, PaperStatus, PaperTopic, Topic
from app.services.collector import Collector
from tests.conftest import PROFILE_ID
from tests.factories import PERIOD_END, PERIOD_START, FakeSource, fake_extractor, make_entry

LLM_QUERY = 'all:"large language model"'
PERIOD_CLAUSE = "submittedDate:[202410050000 TO 202410052359]"


def _topic(
    db: Session,
    name: str,
    queries: list[str],
    is_active: bool = True,
    categories: list[str] | None = None,
) -> Topic:
    topic = Topic(
        profile_id=PROFILE_ID,
        name=name,
        queries=queries,
        arxiv_categories=["cs.CL"] if categories is None else categories,
        is_active=is_active,
    )
    db.add(topic)
    db.commit()
    return topic


def _collector(
    db: Session,
    source: FakeSource,
    tmp_path: Path,
    real_text: bool = False,
    max_results: int = 200,
) -> Collector:
    kwargs = {} if real_text else {"text_extractor": fake_extractor}
    return Collector(
        db,
        source,
        profile_id=PROFILE_ID,
        papers_dir=tmp_path / "papers",
        max_results_per_topic=max_results,
        **kwargs,
    )


def _matched(db: Session, arxiv_id: str) -> dict[int, list[str]]:
    paper = db.scalar(select(Paper).where(Paper.arxiv_id == arxiv_id))
    assert paper is not None
    db.refresh(paper)
    return {link.topic_id: link.matched_queries for link in paper.topic_links}


def test_collect_saves_papers_downloads_pdf_and_extracts_text(db: Session, tmp_path: Path) -> None:
    _topic(db, "LLM", [LLM_QUERY])
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
    # файлы каждого профиля лежат в своём каталоге
    assert (tmp_path / "papers" / str(PROFILE_ID) / "2410.00001.pdf").is_file()

    assert source.queries == [f"({LLM_QUERY}) AND (cat:cs.CL) AND {PERIOD_CLAUSE}"]
    assert stats.as_dict() == {
        "period_start": PERIOD_START.isoformat(),
        "period_end": PERIOD_END.isoformat(),
        "topics_processed": 1,
        "papers_found": 2,
        "papers_new": 2,
        "duplicates": 0,
        "query_errors": 0,
        "download_errors": 0,
        "text_extracted": 2,
        "text_reused": 0,
        "topics": {
            "LLM": {
                "found": 2,
                "new": 2,
                "duplicates": 0,
                "errors": 0,
                "download_errors": 0,
                "zero_result_queries": 0,
                "limit_reached": False,
                "queries": {LLM_QUERY: {"found": 2, "new": 2, "duplicates": 0, "errors": 0}},
            }
        },
    }


def test_each_query_is_searched_separately_and_matches_are_accumulated(
    db: Session, tmp_path: Path
) -> None:
    datasets = "(ti:dataset OR ti:benchmark) AND all:railway"
    topic = _topic(db, "ЖД", ["autonomous train", datasets, "all:GoA4"], categories=[])
    shared = make_entry("2410.00001")
    source = FakeSource(
        {
            "autonomous train": [shared, make_entry("2410.00002")],
            "ti:dataset": [shared, make_entry("2410.00003")],
        }
    )

    stats = _collector(db, source, tmp_path).run(PERIOD_START, PERIOD_END)

    # простая фраза нормализована, у каждого запроса свой вызов arXiv
    assert source.queries == [
        f'(all:"autonomous train") AND {PERIOD_CLAUSE}',
        f"({datasets}) AND {PERIOD_CLAUSE}",
        f"(all:GoA4) AND {PERIOD_CLAUSE}",
    ]
    assert db.scalar(select(func.count()).select_from(Paper)) == 3
    assert _matched(db, "2410.00001") == {topic.id: ['all:"autonomous train"', datasets]}
    assert _matched(db, "2410.00003") == {topic.id: [datasets]}
    assert source.downloads.count(shared.pdf_url) == 1

    assert stats.papers_found == 3
    assert stats.papers_new == 3
    assert stats.duplicates == 0
    assert stats.topics["ЖД"].as_dict() == {
        "found": 3,
        "new": 3,
        "duplicates": 0,
        "errors": 0,
        "download_errors": 0,
        "zero_result_queries": 1,
        "limit_reached": False,
        "queries": {
            'all:"autonomous train"': {"found": 2, "new": 2, "duplicates": 0, "errors": 0},
            datasets: {"found": 2, "new": 1, "duplicates": 1, "errors": 0},
            "all:GoA4": {"found": 0, "new": 0, "duplicates": 0, "errors": 0},
        },
    }


def test_paper_matched_by_several_topics_is_stored_once(db: Session, tmp_path: Path) -> None:
    llm = _topic(db, "LLM", [LLM_QUERY])
    agents = _topic(db, "Агенты", ["all:agents", 'all:"tool use"'])
    shared = make_entry("2410.00001")
    source = FakeSource(
        {
            "large language model": [shared, make_entry("2410.00002")],
            "all:agents": [shared],
            "tool use": [shared],
        }
    )

    stats = _collector(db, source, tmp_path).run(PERIOD_START, PERIOD_END)

    assert db.scalar(select(func.count()).select_from(Paper)) == 2
    assert _matched(db, "2410.00001") == {
        llm.id: [LLM_QUERY],
        agents.id: ["all:agents", 'all:"tool use"'],
    }
    # PDF общей статьи скачан один раз
    assert source.downloads.count(shared.pdf_url) == 1

    assert stats.papers_found == 2
    assert stats.papers_new == 2
    assert stats.duplicates == 1
    assert stats.topics["LLM"].found == 2
    assert stats.topics["LLM"].new == 2
    agents_stats = stats.topics["Агенты"].as_dict()
    assert (agents_stats["found"], agents_stats["new"], agents_stats["duplicates"]) == (1, 0, 1)
    assert agents_stats["queries"]['all:"tool use"'] == {
        "found": 1,
        "new": 0,
        "duplicates": 1,
        "errors": 0,
    }


def test_matched_queries_accumulate_between_runs(db: Session, tmp_path: Path) -> None:
    topic = _topic(db, "LLM", [LLM_QUERY])
    entry = make_entry("2410.00001")
    _collector(db, FakeSource({"large language model": [entry]}), tmp_path).run(
        PERIOD_START, PERIOD_END
    )

    topic.queries = ["ti:LLM"]
    db.commit()
    _collector(db, FakeSource({"ti:LLM": [entry]}), tmp_path).run(PERIOD_START, PERIOD_END)

    assert _matched(db, "2410.00001") == {topic.id: [LLM_QUERY, "ti:LLM"]}


def test_invalid_and_failing_queries_are_skipped(db: Session, tmp_path: Path) -> None:
    _topic(db, "LLM", ["(ti:dataset OR", "all:broken", LLM_QUERY])
    source = FakeSource(
        {"large language model": [make_entry("2410.00001")]}, failing_queries={"all:broken"}
    )

    stats = _collector(db, source, tmp_path).run(PERIOD_START, PERIOD_END)

    # невалидный запрос до arXiv не доходит
    assert len(source.queries) == 2
    assert db.scalar(select(func.count()).select_from(Paper)) == 1
    assert stats.query_errors == 2
    topic_stats = stats.topics["LLM"].as_dict()
    assert topic_stats["errors"] == 2
    assert topic_stats["found"] == 1
    invalid = topic_stats["queries"]["(ti:dataset OR"]
    assert invalid["errors"] == 1
    assert "Незакрытая скобка" in invalid["error"]
    assert "позиция 1:" in invalid["error"]
    assert topic_stats["queries"]["all:broken"]["errors"] == 1
    assert topic_stats["queries"][LLM_QUERY] == {
        "found": 1,
        "new": 1,
        "duplicates": 0,
        "errors": 0,
    }


def test_limit_applies_to_the_whole_topic(db: Session, tmp_path: Path) -> None:
    _topic(db, "LLM", ["all:first", "all:second", "all:third"])
    source = FakeSource(
        {
            "all:first": [make_entry("2410.00001"), make_entry("2410.00002")],
            "all:second": [make_entry(f"2410.0001{i}") for i in range(5)],
            "all:third": [make_entry("2410.00099")],
        }
    )

    stats = _collector(db, source, tmp_path, max_results=3).run(PERIOD_START, PERIOD_END)

    # второму запросу достался только остаток лимита, третий не выполнялся
    assert len(source.queries) == 2
    assert source.limits == [3, 1]
    assert db.scalar(select(func.count()).select_from(Paper)) == 3
    topic_stats = stats.topics["LLM"].as_dict()
    assert topic_stats["found"] == 3
    assert topic_stats["limit_reached"] is True
    assert topic_stats["queries"]["all:second"]["found"] == 1
    assert "лимит" in topic_stats["queries"]["all:third"]["error"]


def test_rerun_is_idempotent(db: Session, tmp_path: Path) -> None:
    _topic(db, "LLM", [LLM_QUERY])
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
    _topic(db, "LLM", [LLM_QUERY])
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
    _topic(db, "LLM", [LLM_QUERY])
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
    _topic(db, "LLM", [LLM_QUERY])

    stats = _collector(db, FakeSource(), tmp_path).run(PERIOD_START, PERIOD_END)

    assert stats.papers_found == 0
    topic_stats = stats.as_dict()["topics"]["LLM"]
    assert topic_stats["found"] == 0
    assert topic_stats["zero_result_queries"] == 1
    assert db.scalar(select(func.count()).select_from(Paper)) == 0


def test_inactive_topics_are_skipped(db: Session, tmp_path: Path) -> None:
    _topic(db, "LLM", [LLM_QUERY], is_active=False)
    source = FakeSource({"large language model": [make_entry("2410.00001")]})

    stats = _collector(db, source, tmp_path).run(PERIOD_START, PERIOD_END)

    assert source.queries == []
    assert stats.papers_found == 0
    assert stats.topics == {}
