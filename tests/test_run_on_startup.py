"""run_on_startup policies and the missed-window rule."""

from datetime import UTC, datetime, timedelta

from catalog.schema import JobConfig
from config import Settings
from scheduler import jobs_to_run_on_startup, nominal_interval_seconds
from state import StateStore

from support import make_job, make_run

NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)


def _job(**overrides: object) -> JobConfig:
    return JobConfig.model_validate(make_job(**overrides))


def test_nominal_interval_for_daily_cron() -> None:
    job = _job(schedule={"cron": "0 2 * * *"})
    assert nominal_interval_seconds(job, Settings(), NOW) == 86400.0


def test_nominal_interval_for_every_sugar() -> None:
    job = _job(schedule={"every": "15m"})
    assert nominal_interval_seconds(job, Settings(), NOW) == 900.0


def test_manual_only_has_no_interval() -> None:
    assert nominal_interval_seconds(_job(), Settings(), NOW) is None


def test_policies(state_store: StateStore) -> None:
    never = _job(id="never", schedule={"cron": "0 2 * * *", "run_on_startup": "never"})
    always = _job(
        id="always",
        source={"type": "local", "label": "b", "path": "/data/local/b"},
        schedule={"cron": "0 2 * * *", "run_on_startup": "always"},
    )
    manual = _job(
        id="manual",
        source={"type": "local", "label": "c", "path": "/data/local/c"},
        schedule={"run_on_startup": "always"},
    )
    due = jobs_to_run_on_startup([never, always, manual], state_store, Settings(), NOW)
    assert [job.id for job in due] == ["always"]


def test_if_missed_fires_without_any_success(state_store: StateStore) -> None:
    job = _job(id="j", schedule={"every": "15m"})
    due = jobs_to_run_on_startup([job], state_store, Settings(), NOW)
    assert [j.id for j in due] == ["j"]


def test_if_missed_skips_recent_success(state_store: StateStore) -> None:
    job = _job(id="j", schedule={"every": "15m"})
    finished = (NOW - timedelta(seconds=600)).isoformat()  # 600 < 1.5 * 900
    state_store.create_run(
        make_run(run_id="r1", job_id="j", status="success", finished_at=finished)
    )
    assert jobs_to_run_on_startup([job], state_store, Settings(), NOW) == []


def test_if_missed_fires_after_the_window(state_store: StateStore) -> None:
    job = _job(id="j", schedule={"every": "15m"})
    finished = (NOW - timedelta(seconds=2000)).isoformat()  # 2000 > 1350
    state_store.create_run(
        make_run(run_id="r1", job_id="j", status="success", finished_at=finished)
    )
    due = jobs_to_run_on_startup([job], state_store, Settings(), NOW)
    assert [j.id for j in due] == ["j"]


def test_disabled_jobs_never_fire(state_store: StateStore) -> None:
    job = _job(id="j", enabled=False, schedule={"cron": "0 2 * * *"})
    assert jobs_to_run_on_startup([job], state_store, Settings(), NOW) == []
