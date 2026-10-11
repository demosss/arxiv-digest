from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.models import Paper, RunStatus, RunTrigger, TaskLog, TaskRun, TaskType, Topic
from app.schemas.runs import RunCreate
from app.services import runs
from app.services.arxiv_client import ArxivUnavailableError
from app.services.periods import previous_utc_day
from app.tasks import pipeline
from app.tasks.celery_app import celery_app
from app.tasks.pipeline import collect_task
from tests.conftest import PROFILE_ID
from tests.factories import PERIOD_END, PERIOD_START, FakeSource, make_entry

PERIOD = {"period_start": PERIOD_START.isoformat(), "period_end": PERIOD_END.isoformat()}


@pytest.fixture
def worker_logging() -> None:
    configure_logging("worker")


@pytest.fixture
def fake_arxiv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeSource:
    """Подменяет arXiv в задаче collect; PDF пишутся во временный каталог."""
    source = FakeSource({"large language model": [make_entry("2410.00001")]})
    monkeypatch.setattr(pipeline, "ArxivClient", lambda *args, **kwargs: source)
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    return source


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Перехватывает публикацию задач в брокер."""
    calls: list[dict[str, Any]] = []

    def fake_apply_async(args: Any = None, kwargs: Any = None, **options: Any) -> None:
        calls.append({"args": args, "kwargs": kwargs, **options})

    monkeypatch.setattr(collect_task, "apply_async", fake_apply_async)
    yield calls


def _run(db: Session, **kwargs: Any) -> TaskRun:
    run = runs.create_run(
        db,
        profile_id=PROFILE_ID,
        task_type=TaskType.COLLECT,
        trigger=RunTrigger.MANUAL,
        params=PERIOD,
        **kwargs,
    )
    db.commit()
    return run


def _topic(db: Session) -> None:
    db.add(
        Topic(
            profile_id=PROFILE_ID,
            name="LLM",
            queries=['all:"large language model"'],
            arxiv_categories=[],
        )
    )
    db.commit()


# --- выполнение шага ---


def test_collect_task_records_status_stats_and_logs(
    db: Session, fake_arxiv: FakeSource, worker_logging: None
) -> None:
    _topic(db)
    run = _run(db)

    result = collect_task.apply(args=[PERIOD], kwargs={"run_id": run.id})

    assert result.successful()
    assert result.result == PERIOD
    db.refresh(run)
    assert run.status == RunStatus.SUCCESS
    assert run.started_at is not None and run.finished_at is not None
    assert run.duration_ms is not None and run.duration_ms >= 0
    assert run.stats["papers_found"] == 1
    assert run.stats["topics"]["LLM"]["new"] == 1
    assert run.error is None

    paper = db.scalar(select(Paper))
    assert paper is not None and paper.text_chars

    logs = db.scalars(select(TaskLog).where(TaskLog.run_id == run.id).order_by(TaskLog.id)).all()
    messages = [entry.message for entry in logs]
    assert messages[0] == "step_started"
    assert "collect_topic_started" in messages
    assert "pdf_text_extracted" in messages
    assert messages[-1] == "step_finished"
    assert all(entry.level in {"info", "warning", "error"} for entry in logs)
    extracted = next(entry for entry in logs if entry.message == "pdf_text_extracted")
    assert extracted.extra["arxiv_id"] == "2410.00001"
    assert extracted.extra["pages"] == 2
    # служебные поля не дублируются в extra
    assert "run_id" not in extracted.extra and "timestamp" not in extracted.extra


def test_collect_task_without_period_uses_previous_utc_day(
    db: Session, fake_arxiv: FakeSource, worker_logging: None
) -> None:
    _topic(db)
    run = _run(db)

    result = collect_task.apply(args=[{}], kwargs={"run_id": run.id})

    start, end = previous_utc_day()
    assert result.result == {"period_start": start.isoformat(), "period_end": end.isoformat()}
    assert start.strftime("%Y%m%d0000") in fake_arxiv.queries[0]


def test_failed_step_stores_error_and_traceback(
    db: Session, fake_arxiv: FakeSource, worker_logging: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _topic(db)
    run = _run(db)

    def broken_search(query: str, max_results: int) -> list[Any]:
        raise ValueError("unexpected feed structure")

    monkeypatch.setattr(fake_arxiv, "search", broken_search)

    result = collect_task.apply(args=[PERIOD], kwargs={"run_id": run.id})

    assert result.failed()
    db.refresh(run)
    assert run.status == RunStatus.FAILED
    assert run.error == "unexpected feed structure"
    assert run.traceback and "ValueError" in run.traceback
    assert run.finished_at is not None

    failure = db.scalar(
        select(TaskLog).where(TaskLog.run_id == run.id, TaskLog.message == "step_failed")
    )
    assert failure is not None
    assert failure.level == "error"
    assert "ValueError" in failure.extra["exception"]


def test_retryable_error_requeues_run(
    db: Session, fake_arxiv: FakeSource, worker_logging: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _topic(db)
    run = _run(db)

    def unavailable(query: str, max_results: int) -> list[Any]:
        raise ArxivUnavailableError("Запрос к arXiv API: HTTP 503 после 4 попыток")

    monkeypatch.setattr(fake_arxiv, "search", unavailable)
    retries: list[dict[str, Any]] = []

    def fake_retry(exc: Exception | None = None, countdown: int = 0, **_: Any) -> Exception:
        retries.append({"exc": exc, "countdown": countdown})
        return RuntimeError("retry scheduled")

    monkeypatch.setattr(collect_task, "retry", fake_retry)

    result = collect_task.apply(args=[PERIOD], kwargs={"run_id": run.id})

    assert result.failed()
    assert retries and retries[0]["countdown"] == 60
    db.refresh(run)
    assert run.status == RunStatus.QUEUED
    assert run.error and run.error.startswith("Повтор 1: ")


def test_cancelled_run_is_not_executed(
    db: Session, fake_arxiv: FakeSource, worker_logging: None
) -> None:
    _topic(db)
    run = _run(db, status=RunStatus.CANCELLED)

    collect_task.apply(args=[PERIOD], kwargs={"run_id": run.id})

    db.refresh(run)
    assert run.status == RunStatus.CANCELLED
    assert fake_arxiv.queries == []


def test_secrets_are_masked_in_task_logs(
    db: Session, fake_arxiv: FakeSource, worker_logging: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _topic(db)
    run = _run(db)
    secret = get_settings().llm_api_key.get_secret_value()

    def leaking_search(query: str, max_results: int) -> list[Any]:
        raise ValueError(f"request failed: api_key={secret}")

    monkeypatch.setattr(fake_arxiv, "search", leaking_search)
    collect_task.apply(args=[PERIOD], kwargs={"run_id": run.id})

    logs = db.scalars(select(TaskLog).where(TaskLog.run_id == run.id)).all()
    assert logs
    assert all(secret not in entry.message for entry in logs)
    assert all(secret not in str(entry.extra) for entry in logs)


# --- статус пайплайна выводится из шагов ---


def _pipeline(db: Session) -> tuple[TaskRun, list[TaskRun]]:
    parent = runs.create_run(
        db, profile_id=PROFILE_ID, task_type=TaskType.FULL_PIPELINE, trigger=RunTrigger.SCHEDULE
    )
    steps = [
        runs.create_run(
            db,
            profile_id=PROFILE_ID,
            task_type=task_type,
            trigger=RunTrigger.SCHEDULE,
            parent_run_id=parent.id,
        )
        for task_type in (
            TaskType.COLLECT,
            TaskType.SUMMARIZE,
            TaskType.BUILD_DIGEST,
            TaskType.SEND_DIGEST,
        )
    ]
    db.commit()
    return parent, steps


def test_pipeline_succeeds_when_all_steps_finish(db: Session) -> None:
    parent, steps = _pipeline(db)

    runs.mark_running(db, steps[0], "task-1")
    db.refresh(parent)
    assert parent.status == RunStatus.RUNNING
    assert parent.started_at is not None

    for step in steps[:3]:
        runs.finish_run(db, step, RunStatus.SUCCESS, stats={"ok": True})
        db.refresh(parent)
        assert parent.status == RunStatus.RUNNING
    runs.finish_run(db, steps[3], RunStatus.SKIPPED, error="Дайджест пуст")

    db.refresh(parent)
    assert parent.status == RunStatus.SUCCESS
    assert parent.finished_at is not None
    assert parent.stats["collect"] == {"ok": True}
    assert set(parent.stats) == {"collect", "summarize", "build_digest", "send_digest"}


def test_failed_step_fails_pipeline_and_skips_the_rest(db: Session) -> None:
    parent, steps = _pipeline(db)
    runs.mark_running(db, steps[0], "task-1")
    runs.finish_run(db, steps[0], RunStatus.SUCCESS)
    runs.mark_running(db, steps[1], "task-2")

    runs.finish_run(db, steps[1], RunStatus.FAILED, error="LLM недоступна")

    for obj in (parent, *steps):
        db.refresh(obj)
    assert parent.status == RunStatus.FAILED
    assert parent.error == "Шаг «summarize»: LLM недоступна"
    assert [s.status for s in steps] == [
        RunStatus.SUCCESS,
        RunStatus.FAILED,
        RunStatus.SKIPPED,
        RunStatus.SKIPPED,
    ]
    assert steps[2].error and "summarize" in steps[2].error


# --- API ---


async def test_manual_run_is_created_and_enqueued(
    auth_client: AsyncClient, published: list[dict[str, Any]]
) -> None:
    response = await auth_client.post(
        "/api/runs",
        json={
            "task_type": "collect",
            "period_start": "2024-10-05T00:00:00Z",
            "period_end": "2024-10-05T23:59:59Z",
        },
    )

    assert response.status_code == 202
    run = response.json()
    assert run["status"] == "queued"
    assert run["trigger"] == "manual"
    assert run["task_type"] == "collect"
    assert run["params"] == {
        "period_start": "2024-10-05T00:00:00+00:00",
        "period_end": "2024-10-05T23:59:59+00:00",
    }
    assert run["children"] == []

    assert len(published) == 1
    assert published[0]["args"] == [run["params"]]
    assert published[0]["kwargs"] == {"run_id": run["id"]}
    assert published[0]["task_id"] == run["celery_task_id"]


async def test_manual_run_defaults_to_previous_utc_day(
    auth_client: AsyncClient, published: list[dict[str, Any]]
) -> None:
    response = await auth_client.post("/api/runs", json={"task_type": "collect"})

    start, end = previous_utc_day()
    assert response.json()["params"] == {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"task_type": "collect", "period_start": "2024-10-05T00:00:00Z"},
        {
            "task_type": "collect",
            "period_start": "2024-10-06T00:00:00Z",
            "period_end": "2024-10-05T00:00:00Z",
        },
        {
            "task_type": "collect",
            "period_start": "2024-10-05T00:00:00",
            "period_end": "2024-10-05T23:59:59",
        },
        {"task_type": "dance"},
    ],
)
async def test_manual_run_validation(
    auth_client: AsyncClient, published: list[dict[str, Any]], payload: dict[str, str]
) -> None:
    response = await auth_client.post("/api/runs", json=payload)

    assert response.status_code == 422
    assert published == []


def test_step_specific_params_reach_only_their_tasks() -> None:
    def params(task_type: str, **extra: Any) -> dict[str, Any]:
        return RunCreate.model_validate({"task_type": task_type, **extra}).to_params()

    assert params("build_digest", digest_content="abstract")["digest_content"] == "abstract"
    assert params("full_pipeline", digest_content="full")["digest_content"] == "full"
    assert "digest_content" not in params("build_digest")
    assert "digest_content" not in params("collect", digest_content="abstract")
    assert params("summarize", fill_abstracts=True) == {"fill_abstracts": True}
    # в цепочке флаг подменил бы суммаризацию новых статей
    assert "fill_abstracts" not in params("full_pipeline", fill_abstracts=True)
    with pytest.raises(ValueError, match="digest_content"):
        params("build_digest", digest_content="everything")


async def test_broker_failure_marks_run_failed(
    auth_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise ConnectionRefusedError("broker is down")

    monkeypatch.setattr(collect_task, "apply_async", broken)

    response = await auth_client.post("/api/runs", json={"task_type": "collect"})

    assert response.status_code == 202
    assert response.json()["status"] == "failed"
    assert "Не удалось поставить задачу в очередь" in response.json()["error"]


async def test_runs_list_filters(auth_client: AsyncClient, db: Session) -> None:
    parent, _ = _pipeline(db)
    manual = _run(db)
    runs.finish_run(db, manual, RunStatus.FAILED, error="boom")

    top_level = (await auth_client.get("/api/runs")).json()
    assert top_level["total"] == 2
    assert [r["id"] for r in top_level["items"]] == [manual.id, parent.id]

    with_steps = (await auth_client.get("/api/runs", params={"include_steps": True})).json()
    assert with_steps["total"] == 6

    failed = (await auth_client.get("/api/runs", params={"status": "failed"})).json()
    assert [r["id"] for r in failed["items"]] == [manual.id]

    by_type = (await auth_client.get("/api/runs", params={"task_type": "full_pipeline"})).json()
    assert [r["id"] for r in by_type["items"]] == [parent.id]

    scheduled = (await auth_client.get("/api/runs", params={"trigger": "schedule"})).json()
    assert [r["id"] for r in scheduled["items"]] == [parent.id]

    future = (
        await auth_client.get("/api/runs", params={"date_from": "2999-01-01T00:00:00Z"})
    ).json()
    assert future["total"] == 0


async def test_run_detail_includes_steps(auth_client: AsyncClient, db: Session) -> None:
    parent, _ = _pipeline(db)

    response = await auth_client.get(f"/api/runs/{parent.id}")

    assert response.status_code == 200
    assert [child["task_type"] for child in response.json()["children"]] == [
        "collect",
        "summarize",
        "build_digest",
        "send_digest",
    ]
    assert (await auth_client.get("/api/runs/999999")).status_code == 404


async def test_run_logs_with_level_filter_and_after_id(
    auth_client: AsyncClient, db: Session
) -> None:
    parent, steps = _pipeline(db)
    other = _run(db)
    rows = [
        TaskLog(run_id=steps[0].id, level="info", logger="app", message="collect_started"),
        TaskLog(run_id=steps[0].id, level="warning", logger="app", message="arxiv_retry"),
        TaskLog(run_id=steps[1].id, level="error", logger="app", message="llm_failed"),
        TaskLog(run_id=other.id, level="error", logger="app", message="other_run"),
    ]
    db.add_all(rows)
    db.commit()

    all_logs = (await auth_client.get(f"/api/runs/{parent.id}/logs")).json()
    assert [entry["message"] for entry in all_logs["items"]] == [
        "collect_started",
        "arxiv_retry",
        "llm_failed",
    ]
    assert all_logs["total"] == 3
    assert all_logs["last_id"] == rows[2].id

    warnings = (
        await auth_client.get(f"/api/runs/{parent.id}/logs", params={"level": "warning"})
    ).json()
    assert [entry["message"] for entry in warnings["items"]] == ["arxiv_retry", "llm_failed"]

    step_only = (await auth_client.get(f"/api/runs/{steps[0].id}/logs")).json()
    assert step_only["total"] == 2

    newer = (
        await auth_client.get(f"/api/runs/{parent.id}/logs", params={"after_id": rows[1].id})
    ).json()
    assert [entry["message"] for entry in newer["items"]] == ["llm_failed"]

    own_only = (
        await auth_client.get(f"/api/runs/{parent.id}/logs", params={"include_steps": False})
    ).json()
    assert own_only["items"] == [] and own_only["last_id"] is None


async def test_global_log_search(auth_client: AsyncClient, db: Session) -> None:
    first, second = _run(db), _run(db)
    db.add_all(
        [
            TaskLog(
                run_id=first.id,
                level="info",
                logger="app.services.arxiv_client",
                message="arxiv_page",
            ),
            TaskLog(
                run_id=first.id,
                level="error",
                logger="app.services.collector",
                message="paper_processing_failed",
            ),
            TaskLog(
                run_id=second.id,
                level="info",
                logger="app.services.arxiv_client",
                message="arxiv_page",
            ),
        ]
    )
    db.commit()

    found = (await auth_client.get("/api/logs", params={"q": "ARXIV"})).json()
    assert found["total"] == 2
    # новые записи сверху
    assert found["items"][0]["run_id"] == second.id

    errors = (await auth_client.get("/api/logs", params={"level": "error"})).json()
    assert [entry["message"] for entry in errors["items"]] == ["paper_processing_failed"]

    by_run = (await auth_client.get("/api/logs", params={"run_id": first.id})).json()
    assert by_run["total"] == 2

    by_logger = (
        await auth_client.get("/api/logs", params={"logger": "app.services.collector"})
    ).json()
    assert by_logger["total"] == 1

    by_type = (await auth_client.get("/api/logs", params={"task_type": "summarize"})).json()
    assert by_type["total"] == 0


async def test_cancel_run(
    auth_client: AsyncClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    revoked: list[str] = []
    monkeypatch.setattr(
        celery_app.control, "revoke", lambda task_id, **kwargs: revoked.append(task_id)
    )
    parent, steps = _pipeline(db)
    runs.mark_running(db, steps[0], "celery-task-1")

    response = await auth_client.post(f"/api/runs/{steps[0].id}/cancel")

    assert response.status_code == 200
    assert response.json()["run"]["status"] == "cancelled"
    assert response.json()["cancelled_run_ids"] == [steps[0].id]
    assert revoked == ["celery-task-1"]

    detail = (await auth_client.get(f"/api/runs/{parent.id}")).json()
    assert detail["status"] == "cancelled"
    assert [child["status"] for child in detail["children"]] == [
        "cancelled",
        "skipped",
        "skipped",
        "skipped",
    ]

    again = await auth_client.post(f"/api/runs/{steps[0].id}/cancel")
    assert again.status_code == 409
    assert again.json()["code"] == "run_not_active"


async def test_delete_run_removes_steps_and_logs(auth_client: AsyncClient, db: Session) -> None:
    parent, steps = _pipeline(db)
    db.add(TaskLog(run_id=steps[0].id, level="info", logger="app", message="collect_started"))
    db.commit()

    active = await auth_client.delete(f"/api/runs/{parent.id}")
    assert active.status_code == 409
    assert active.json()["code"] == "run_active"

    for step in steps:
        runs.finish_run(db, step, RunStatus.SUCCESS)

    parent_id = parent.id
    assert (await auth_client.delete(f"/api/runs/{parent_id}")).status_code == 204
    db.expire_all()
    assert db.scalars(select(TaskRun)).all() == []
    assert db.scalars(select(TaskLog)).all() == []
    assert (await auth_client.delete(f"/api/runs/{parent_id}")).status_code == 404


async def test_clear_runs_respects_filters_and_keeps_active(
    auth_client: AsyncClient, db: Session
) -> None:
    active_pipeline, _ = _pipeline(db)
    failed, done, queued = _run(db), _run(db), _run(db)
    runs.finish_run(db, failed, RunStatus.FAILED, error="boom")
    runs.finish_run(db, done, RunStatus.SUCCESS)

    only_failed = await auth_client.delete("/api/runs", params={"status": "failed"})
    assert only_failed.status_code == 200
    assert only_failed.json() == {"deleted": 1}

    assert (await auth_client.delete("/api/runs")).json() == {"deleted": 1}

    remaining = (await auth_client.get("/api/runs")).json()
    assert [r["id"] for r in remaining["items"]] == [queued.id, active_pipeline.id]
    assert (await auth_client.get(f"/api/runs/{active_pipeline.id}")).json()["children"]
