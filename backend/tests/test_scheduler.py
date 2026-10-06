import smtplib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import dashboard as dashboard_api
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.models import (
    SMTP_SETTINGS_ID,
    Digest,
    DigestDelivery,
    DigestStatus,
    Paper,
    PaperStatus,
    Recipient,
    RunStatus,
    RunTrigger,
    Schedule,
    SmtpSecurity,
    SmtpSettings,
    TaskLog,
    TaskRun,
    TaskType,
    Topic,
)
from app.schemas.papers import LLMHealthOut
from app.services import runs
from app.services.llm_client import LLMClient
from app.services.periods import previous_utc_day
from app.services.scheduler import last_fire_time, next_fire_time, tick
from app.tasks import launcher, pipeline
from app.tasks.celery_app import celery_app
from app.tasks.scheduler_tick import scheduler_tick
from tests.factories import FakeLLM, FakeSource, make_entry
from tests.test_mailer import RecordingSMTP, RecordingSMTPSSL

NOW = datetime(2024, 10, 7, 6, 0, 30, tzinfo=UTC)  # понедельник, 09:00:30 по Москве


class FakeLauncher:
    """Вместо постановки в очередь создаёт запись запуска и запоминает вызов."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, session: Session, **kwargs: Any) -> TaskRun:
        self.calls.append(kwargs)
        run = runs.create_run(session, **kwargs)
        session.commit()
        return run


def _schedule(
    db: Session,
    cron: str = "0 9 * * *",
    timezone: str = "Europe/Moscow",
    task_type: TaskType = TaskType.FULL_PIPELINE,
    *,
    next_fire_at: datetime | None,
    last_fired_at: datetime | None = None,
    is_active: bool = True,
) -> Schedule:
    schedule = Schedule(
        name=f"Расписание {cron}",
        task_type=task_type,
        cron_expression=cron,
        timezone=timezone,
        is_active=is_active,
        next_fire_at=next_fire_at,
        last_fired_at=last_fired_at,
    )
    db.add(schedule)
    db.commit()
    return schedule


# --- расчёт времени срабатываний ---


def test_last_and_next_fire_time_respect_timezone() -> None:
    assert last_fire_time("0 9 * * *", "Europe/Moscow", NOW) == datetime(
        2024, 10, 7, 6, 0, tzinfo=UTC
    )
    assert next_fire_time("0 9 * * *", "Europe/Moscow", NOW) == datetime(
        2024, 10, 8, 6, 0, tzinfo=UTC
    )
    # то же выражение в UTC ещё не наступило сегодня
    assert last_fire_time("0 9 * * *", "UTC", NOW) == datetime(2024, 10, 6, 9, 0, tzinfo=UTC)
    assert next_fire_time("0 9 * * *", "UTC", NOW) == datetime(2024, 10, 7, 9, 0, tzinfo=UTC)


def test_fire_time_across_dst_change() -> None:
    # в Берлине 27.10.2024 часы переводятся назад: 09:00 местного — это уже 08:00 UTC
    before = datetime(2024, 10, 26, 12, 0, tzinfo=UTC)
    first = next_fire_time("0 9 * * *", "Europe/Berlin", before)
    second = next_fire_time("0 9 * * *", "Europe/Berlin", first)

    assert first == datetime(2024, 10, 27, 8, 0, tzinfo=UTC)
    assert second == datetime(2024, 10, 28, 8, 0, tzinfo=UTC)


# --- тик ---


def test_due_schedule_fires_once(db: Session) -> None:
    due_at = datetime(2024, 10, 7, 6, 0, tzinfo=UTC)
    schedule = _schedule(db, next_fire_at=due_at)
    launch = FakeLauncher()

    fired = tick(db, launch, NOW)

    assert len(fired) == 1
    start, end = previous_utc_day()
    assert launch.calls == [
        {
            "task_type": TaskType.FULL_PIPELINE,
            "trigger": RunTrigger.SCHEDULE,
            "params": {"period_start": start.isoformat(), "period_end": end.isoformat()},
            "schedule_id": schedule.id,
        }
    ]
    db.refresh(schedule)
    assert schedule.last_fired_at == due_at
    assert schedule.next_fire_at == datetime(2024, 10, 8, 6, 0, tzinfo=UTC)
    run = db.scalar(select(TaskRun))
    assert run is not None
    assert run.trigger == RunTrigger.SCHEDULE
    assert run.schedule_id == schedule.id


def test_schedule_does_not_fire_twice(db: Session) -> None:
    _schedule(db, next_fire_at=datetime(2024, 10, 7, 6, 0, tzinfo=UTC))
    launch = FakeLauncher()

    tick(db, launch, NOW)
    tick(db, launch, NOW)
    tick(db, launch, NOW + timedelta(seconds=40))
    tick(db, launch, NOW + timedelta(hours=5))

    assert len(launch.calls) == 1


def test_every_five_minutes_fires_exactly_once_per_interval(db: Session) -> None:
    start = datetime(2024, 10, 7, 6, 0, tzinfo=UTC)
    _schedule(db, cron="*/5 * * * *", task_type=TaskType.COLLECT, next_fire_at=start)
    launch = FakeLauncher()
    fired_at: list[datetime] = []

    # полчаса тиков раз в минуту, как это делает beat
    for minute in range(31):
        now = start + timedelta(minutes=minute, seconds=7)
        if tick(db, launch, now):
            fired_at.append(now.replace(second=0))

    assert fired_at == [start + timedelta(minutes=m) for m in range(0, 31, 5)]


def test_schedule_not_due_yet(db: Session) -> None:
    schedule = _schedule(db, next_fire_at=datetime(2024, 10, 7, 6, 1, tzinfo=UTC))
    launch = FakeLauncher()

    assert tick(db, launch, NOW) == []
    assert launch.calls == []
    db.refresh(schedule)
    assert schedule.last_fired_at is None


def test_missed_firings_are_not_replayed(db: Session, capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("worker")
    # сервис лежал три часа: пропущены запуски в 03:00, 04:00 и 05:00 UTC
    schedule = _schedule(
        db,
        cron="0 * * * *",
        timezone="UTC",
        next_fire_at=datetime(2024, 10, 7, 3, 0, tzinfo=UTC),
        last_fired_at=datetime(2024, 10, 7, 2, 0, tzinfo=UTC),
    )
    launch = FakeLauncher()

    fired = tick(db, launch, NOW)

    assert len(fired) == 1
    db.refresh(schedule)
    assert schedule.last_fired_at == datetime(2024, 10, 7, 6, 0, tzinfo=UTC)
    assert schedule.next_fire_at == datetime(2024, 10, 7, 7, 0, tzinfo=UTC)
    output = capsys.readouterr().out
    assert "schedule_missed_firings" in output
    assert "2024-10-07T03:00:00+00:00" in output


def test_schedule_timezone_decides_when_it_is_due(db: Session) -> None:
    moscow = _schedule(db, timezone="Europe/Moscow", next_fire_at=None)
    utc = _schedule(db, timezone="UTC", next_fire_at=None)
    for schedule in (moscow, utc):
        schedule.next_fire_at = next_fire_time(
            schedule.cron_expression, schedule.timezone, datetime(2024, 10, 7, 0, 0, tzinfo=UTC)
        )
    db.commit()
    launch = FakeLauncher()

    # 06:00 UTC = 09:00 по Москве: срабатывает только московское расписание
    tick(db, launch, NOW)
    assert [call["schedule_id"] for call in launch.calls] == [moscow.id]

    # 09:00 UTC: очередь расписания в UTC
    tick(db, launch, datetime(2024, 10, 7, 9, 0, 10, tzinfo=UTC))
    assert [call["schedule_id"] for call in launch.calls] == [moscow.id, utc.id]


def test_inactive_schedule_is_ignored(db: Session) -> None:
    _schedule(db, next_fire_at=datetime(2024, 10, 7, 6, 0, tzinfo=UTC), is_active=False)
    launch = FakeLauncher()

    assert tick(db, launch, NOW) == []


def test_schedule_without_next_fire_at_is_rescheduled_not_fired(db: Session) -> None:
    schedule = _schedule(db, next_fire_at=None)
    launch = FakeLauncher()

    assert tick(db, launch, NOW) == []
    db.refresh(schedule)
    assert schedule.next_fire_at == datetime(2024, 10, 8, 6, 0, tzinfo=UTC)


def test_already_fired_slot_is_skipped(db: Session) -> None:
    slot = datetime(2024, 10, 7, 6, 0, tzinfo=UTC)
    schedule = _schedule(db, next_fire_at=slot, last_fired_at=slot)
    launch = FakeLauncher()

    assert tick(db, launch, NOW) == []
    db.refresh(schedule)
    assert schedule.next_fire_at == datetime(2024, 10, 8, 6, 0, tzinfo=UTC)


def test_invalid_schedule_is_deactivated(db: Session) -> None:
    broken = _schedule(db, cron="not a cron", next_fire_at=NOW - timedelta(minutes=1))
    good = _schedule(db, next_fire_at=datetime(2024, 10, 7, 6, 0, tzinfo=UTC))
    launch = FakeLauncher()

    fired = tick(db, launch, NOW)

    assert [call["schedule_id"] for call in launch.calls] == [good.id]
    assert len(fired) == 1
    db.refresh(broken)
    assert broken.is_active is False


def test_step_schedule_has_no_period_for_summarize(db: Session) -> None:
    _schedule(
        db, task_type=TaskType.SUMMARIZE, next_fire_at=datetime(2024, 10, 7, 6, 0, tzinfo=UTC)
    )
    launch = FakeLauncher()

    tick(db, launch, NOW)

    assert launch.calls[0]["task_type"] == TaskType.SUMMARIZE
    assert launch.calls[0]["params"] == {}


# --- запуск пайплайна и защита от параллельного запуска ---


@pytest.fixture
def chains(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    published: list[dict[str, Any]] = []

    def fake_publish(steps: list[TaskRun], params: dict[str, Any]) -> None:
        published.append({"steps": [s.task_type for s in steps], "params": params})

    monkeypatch.setattr(launcher, "publish_pipeline", fake_publish)
    return published


def test_pipeline_launch_creates_parent_and_four_steps(
    db: Session, chains: list[dict[str, Any]]
) -> None:
    params = {
        "period_start": "2024-10-05T00:00:00+00:00",
        "period_end": "2024-10-05T23:59:59+00:00",
    }

    parent = launcher.launch_run(
        db, task_type=TaskType.FULL_PIPELINE, trigger=RunTrigger.MANUAL, params=params
    )

    assert parent.status == RunStatus.QUEUED
    assert parent.params == params
    assert [(c.task_type, c.status) for c in parent.children] == [
        (TaskType.COLLECT, RunStatus.QUEUED),
        (TaskType.SUMMARIZE, RunStatus.QUEUED),
        (TaskType.BUILD_DIGEST, RunStatus.QUEUED),
        (TaskType.SEND_DIGEST, RunStatus.QUEUED),
    ]
    assert len({c.celery_task_id for c in parent.children}) == 4
    assert chains == [
        {
            "steps": [
                TaskType.COLLECT,
                TaskType.SUMMARIZE,
                TaskType.BUILD_DIGEST,
                TaskType.SEND_DIGEST,
            ],
            "params": params,
        }
    ]


def test_parallel_pipeline_is_skipped(
    db: Session, chains: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("worker")
    first = launcher.launch_run(db, task_type=TaskType.FULL_PIPELINE, trigger=RunTrigger.SCHEDULE)

    second = launcher.launch_run(db, task_type=TaskType.FULL_PIPELINE, trigger=RunTrigger.MANUAL)

    assert second.status == RunStatus.SKIPPED
    assert second.error == (
        f"Пайплайн уже выполняется (запуск #{first.id}), параллельный запуск пропущен"
    )
    assert second.finished_at is not None
    assert second.children == []
    assert len(chains) == 1
    assert "pipeline_skipped_already_running" in capsys.readouterr().out

    # после завершения первого пайплайна новый запуск снова возможен
    for step in first.children:
        runs.finish_run(db, step, RunStatus.SUCCESS)
    third = launcher.launch_run(db, task_type=TaskType.FULL_PIPELINE, trigger=RunTrigger.MANUAL)
    assert third.status == RunStatus.QUEUED
    assert len(chains) == 2


def test_single_steps_do_not_block_each_other(
    db: Session, chains: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline.collect_task, "apply_async", lambda *args, **kwargs: None)
    launcher.launch_run(db, task_type=TaskType.FULL_PIPELINE, trigger=RunTrigger.SCHEDULE)

    step = launcher.launch_run(db, task_type=TaskType.COLLECT, trigger=RunTrigger.MANUAL)

    assert step.status == RunStatus.QUEUED


def test_stale_pipeline_is_failed_and_does_not_block(
    db: Session, chains: list[dict[str, Any]]
) -> None:
    stale = launcher.launch_run(db, task_type=TaskType.FULL_PIPELINE, trigger=RunTrigger.SCHEDULE)
    runs.mark_running(db, stale.children[0], "task-1")
    old = datetime.now(UTC) - timedelta(hours=get_settings().pipeline_stale_hours + 1)
    for member in (stale, *stale.children):
        db.execute(TaskRun.__table__.update().where(TaskRun.id == member.id).values(updated_at=old))
    db.commit()
    db.expire_all()

    fresh = launcher.launch_run(db, task_type=TaskType.FULL_PIPELINE, trigger=RunTrigger.SCHEDULE)

    assert fresh.status == RunStatus.QUEUED
    db.refresh(stale)
    assert stale.status == RunStatus.FAILED
    assert stale.error and "завис" in stale.error
    assert {c.status for c in stale.children} == {RunStatus.FAILED}


def test_pipeline_with_recent_logs_is_not_stale(db: Session, chains: list[dict[str, Any]]) -> None:
    active = launcher.launch_run(db, task_type=TaskType.FULL_PIPELINE, trigger=RunTrigger.SCHEDULE)
    runs.mark_running(db, active.children[1], "task-2")
    old = datetime.now(UTC) - timedelta(hours=get_settings().pipeline_stale_hours + 1)
    for member in (active, *active.children):
        db.execute(TaskRun.__table__.update().where(TaskRun.id == member.id).values(updated_at=old))
    # долгая суммаризация: запуск давно не менялся, но в лог пишет
    db.add(TaskLog(run_id=active.children[1].id, level="info", logger="app", message="llm_call"))
    db.commit()
    db.expire_all()

    assert runs.fail_stale_runs(db, get_settings().pipeline_stale_hours) == []
    second = launcher.launch_run(db, task_type=TaskType.FULL_PIPELINE, trigger=RunTrigger.SCHEDULE)
    assert second.status == RunStatus.SKIPPED


def test_broker_failure_fails_pipeline_and_steps(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(steps: list[TaskRun], params: dict[str, Any]) -> None:
        raise ConnectionRefusedError("broker is down")

    monkeypatch.setattr(launcher, "publish_pipeline", broken)

    parent = launcher.launch_run(db, task_type=TaskType.FULL_PIPELINE, trigger=RunTrigger.MANUAL)

    assert parent.status == RunStatus.FAILED
    assert parent.error and "Не удалось поставить задачу в очередь" in parent.error
    # упавший запуск не блокирует следующий
    monkeypatch.setattr(launcher, "publish_pipeline", lambda steps, params: None)
    assert (
        launcher.launch_run(db, task_type=TaskType.FULL_PIPELINE, trigger=RunTrigger.MANUAL).status
        == RunStatus.QUEUED
    )


# --- сквозной прогон: расписание → цепочка из четырёх шагов ---


@pytest.fixture
def eager_celery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(celery_app.conf, "task_eager_propagates", False)


def test_scheduled_full_pipeline_end_to_end(
    db: Session, tmp_path: Path, eager_celery: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_logging("worker")
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    period_start, _ = previous_utc_day()
    entry = make_entry("2410.00001", title="Agents That Plan")
    object.__setattr__(entry, "published_at", period_start + timedelta(hours=12))
    source = FakeSource({"large language model": [entry]})
    monkeypatch.setattr(pipeline, "ArxivClient", lambda *args, **kwargs: source)
    monkeypatch.setattr(LLMClient, "from_settings", classmethod(lambda cls, settings: FakeLLM()))
    RecordingSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", RecordingSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", RecordingSMTPSSL)

    db.add_all(
        [
            Topic(name="LLM", keywords=["large language model"]),
            Recipient(email="reader@example.com"),
            SmtpSettings(
                id=SMTP_SETTINGS_ID,
                host="smtp.test",
                port=1025,
                from_email="digest@example.com",
                security=SmtpSecurity.NONE,
            ),
        ]
    )
    schedule = _schedule(db, cron="*/5 * * * *", next_fire_at=datetime.now(UTC))

    assert scheduler_tick.apply().result == 1
    # повторный тик в том же интервале ничего не запускает
    assert scheduler_tick.apply().result == 0

    parent = db.scalar(select(TaskRun).where(TaskRun.task_type == TaskType.FULL_PIPELINE))
    assert parent is not None
    assert parent.trigger == RunTrigger.SCHEDULE
    assert parent.schedule_id == schedule.id
    assert parent.status == RunStatus.SUCCESS, parent.error
    assert [(c.task_type, c.status) for c in parent.children] == [
        (TaskType.COLLECT, RunStatus.SUCCESS),
        (TaskType.SUMMARIZE, RunStatus.SUCCESS),
        (TaskType.BUILD_DIGEST, RunStatus.SUCCESS),
        (TaskType.SEND_DIGEST, RunStatus.SUCCESS),
    ]
    assert parent.stats["collect"]["papers_new"] == 1
    assert parent.stats["summarize"]["summarized"] == 1
    assert parent.stats["build_digest"]["papers_count"] == 1
    assert parent.stats["send_digest"]["sent"] == 1

    paper = db.scalar(select(Paper))
    digest = db.scalar(select(Digest))
    delivery = db.scalar(select(DigestDelivery))
    assert paper is not None and paper.status == PaperStatus.SUMMARIZED
    assert digest is not None and digest.status == DigestStatus.SENT
    assert digest.papers_count == 1
    assert delivery is not None and delivery.email == "reader@example.com"
    sends = [call for call in RecordingSMTP.instances[0].calls if call[0] == "send"]
    assert len(sends) == 1

    # логи всех шагов доступны через родительский запуск
    step_ids = [c.id for c in parent.children]
    messages = db.scalars(select(TaskLog.message).where(TaskLog.run_id.in_(step_ids))).all()
    for expected in ("collect_done", "paper_summarized", "digest_built", "mail_sent"):
        assert expected in messages


def test_failed_step_stops_the_chain(
    db: Session, tmp_path: Path, eager_celery: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_logging("worker")
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)

    class BrokenSource(FakeSource):
        def search(self, query: str, max_results: int) -> list[Any]:
            raise ValueError("arXiv вернул неожиданный ответ")

    monkeypatch.setattr(pipeline, "ArxivClient", lambda *args, **kwargs: BrokenSource())
    db.add(Topic(name="LLM", keywords=["large language model"]))
    db.commit()

    parent = launcher.launch_run(db, task_type=TaskType.FULL_PIPELINE, trigger=RunTrigger.MANUAL)

    db.refresh(parent)
    assert parent.status == RunStatus.FAILED
    assert parent.error == "Шаг «collect»: arXiv вернул неожиданный ответ"
    assert [c.status for c in parent.children] == [
        RunStatus.FAILED,
        RunStatus.SKIPPED,
        RunStatus.SKIPPED,
        RunStatus.SKIPPED,
    ]


# --- дашборд ---


async def test_dashboard(
    auth_client: AsyncClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    dashboard_api.reset_llm_cache()
    checks: list[int] = []

    def fake_llm() -> LLMHealthOut:
        checks.append(1)
        return LLMHealthOut(
            ok=True,
            base_url="http://llm.test/v1",
            model="qwen-test",
            latency_ms=12,
            model_available=True,
            models=["qwen-test"],
            error=None,
        )

    monkeypatch.setattr(dashboard_api.health, "check_llm", fake_llm)
    soon = _schedule(db, next_fire_at=datetime.now(UTC) + timedelta(hours=1))
    _schedule(db, next_fire_at=datetime.now(UTC) + timedelta(hours=3), is_active=False)
    done = runs.create_run(db, task_type=TaskType.COLLECT, trigger=RunTrigger.MANUAL)
    runs.finish_run(db, done, RunStatus.SUCCESS)
    failed = runs.create_run(db, task_type=TaskType.SUMMARIZE, trigger=RunTrigger.MANUAL)
    runs.finish_run(db, failed, RunStatus.FAILED, error="boom")
    running = runs.create_run(db, task_type=TaskType.FULL_PIPELINE, trigger=RunTrigger.SCHEDULE)
    db.add(
        Digest(
            period_start=NOW,
            period_end=NOW + timedelta(hours=1),
            status=DigestStatus.SENT,
            papers_count=3,
        )
    )
    db.commit()

    response = await auth_client.get("/api/dashboard")

    assert response.status_code == 200
    body = response.json()
    assert body["counters"] == {
        "papers_collected": 0,
        "papers_summarized": 0,
        "digests_sent": 1,
        "runs_success": 1,
        "runs_failed": 1,
    }
    assert body["llm"]["ok"] is True
    assert body["smtp"] == {
        "ok": False,
        "is_configured": False,
        "password_set": True,
        "host": "",
        "error": "Не заданы сервер и адрес отправителя",
    }
    assert body["pipeline_running"] is True
    assert [s["id"] for s in body["upcoming_schedules"]] == [soon.id]
    assert [r["id"] for r in body["recent_runs"]] == [running.id, failed.id, done.id]
    assert body["timezone"] == "Europe/Moscow"

    # состояние LLM кешируется: второй запрос не дёргает модель
    await auth_client.get("/api/dashboard")
    assert len(checks) == 1
    dashboard_api.reset_llm_cache()
