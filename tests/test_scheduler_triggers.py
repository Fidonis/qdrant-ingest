"""Catalog-to-scheduler diffing."""

import time
from collections.abc import Iterator

import pytest

from catalog.schema import JobConfig
from config import Settings
from scheduler import IngestScheduler
from scheduler.aps import build_trigger

from support import make_job


def _job(**overrides: object) -> JobConfig:
    return JobConfig.model_validate(make_job(**overrides))


@pytest.fixture
def scheduler() -> Iterator[IngestScheduler]:
    fired: list[str] = []
    instance = IngestScheduler(Settings(), execute=lambda job: fired.append(job.id))
    instance.start()
    yield instance
    instance.shutdown()


def test_build_trigger_variants() -> None:
    settings = Settings()
    assert build_trigger(_job(schedule={"cron": "0 2 * * *"}), settings) is not None
    assert build_trigger(_job(schedule={"every": "15m"}), settings) is not None
    assert build_trigger(_job(), settings) is None  # manual-only


def test_apply_catalog_schedules_cron_and_interval(scheduler: IngestScheduler) -> None:
    jobs = [
        _job(id="nightly", schedule={"cron": "0 2 * * *"}),
        _job(
            id="quarter-hourly",
            source={"type": "local", "label": "other", "path": "/data/local/b"},
            schedule={"every": "15m"},
        ),
    ]
    scheduler.apply_catalog(jobs)
    assert scheduler.scheduled_ids() == {"nightly", "quarter-hourly"}
    assert scheduler.next_run_time("nightly") is not None
    assert scheduler.next_run_time("quarter-hourly") is not None


def test_manual_only_and_disabled_jobs_are_not_scheduled(
    scheduler: IngestScheduler,
) -> None:
    jobs = [
        _job(id="manual"),
        _job(
            id="disabled",
            enabled=False,
            source={"type": "local", "label": "other", "path": "/data/local/b"},
            schedule={"cron": "0 2 * * *"},
        ),
    ]
    scheduler.apply_catalog(jobs)
    assert scheduler.scheduled_ids() == set()


def test_reapply_removes_vanished_jobs(scheduler: IngestScheduler) -> None:
    scheduler.apply_catalog([_job(id="stays", schedule={"cron": "0 2 * * *"})])
    assert scheduler.scheduled_ids() == {"stays"}
    scheduler.apply_catalog([])
    assert scheduler.scheduled_ids() == set()


def test_reapply_replaces_the_trigger(scheduler: IngestScheduler) -> None:
    scheduler.apply_catalog([_job(id="j", schedule={"cron": "0 2 * * *"})])
    first = scheduler.next_run_time("j")
    scheduler.apply_catalog([_job(id="j", schedule={"cron": "0 4 * * *"})])
    second = scheduler.next_run_time("j")
    assert first is not None and second is not None
    assert first != second


def test_reapply_keeps_the_interval_timer_of_an_unchanged_schedule(
    scheduler: IngestScheduler,
) -> None:
    """An IntervalTrigger counts from its creation, so re-adding it moves the next run."""
    scheduler.apply_catalog([_job(id="j", schedule={"every": "1h"})])
    first = scheduler.next_run_time("j")
    assert first is not None
    time.sleep(0.05)

    # Nothing about the schedule changed: another field of the job, and a new neighbour.
    scheduler.apply_catalog(
        [
            _job(id="j", description="edited", schedule={"every": "1h"}),
            _job(
                id="neighbour",
                source={"type": "local", "label": "other", "path": "/data/local/b"},
                schedule={"every": "1h"},
            ),
        ]
    )

    assert scheduler.next_run_time("j") == first
    assert scheduler.scheduled_ids() == {"j", "neighbour"}


def test_a_changed_interval_does_start_over(scheduler: IngestScheduler) -> None:
    scheduler.apply_catalog([_job(id="j", schedule={"every": "1h"})])
    first = scheduler.next_run_time("j")
    scheduler.apply_catalog([_job(id="j", schedule={"every": "2h"})])
    second = scheduler.next_run_time("j")
    assert first is not None and second is not None
    assert second > first


def test_a_paused_job_stays_paused_when_its_schedule_is_unchanged(
    scheduler: IngestScheduler,
) -> None:
    scheduler.apply_catalog([_job(id="j", schedule={"cron": "0 2 * * *"})])
    scheduler.pause_job("j")

    scheduler.apply_catalog([_job(id="j", schedule={"cron": "0 2 * * *"})])

    assert scheduler.scheduled_ids() == {"j"}
    assert scheduler.next_run_time("j") is None  # still paused
    assert scheduler.resume_job("j") is True
    assert scheduler.next_run_time("j") is not None


def test_pause_and_resume(scheduler: IngestScheduler) -> None:
    scheduler.apply_catalog([_job(id="j", schedule={"cron": "0 2 * * *"})])
    assert scheduler.pause_job("j") is True
    assert scheduler.next_run_time("j") is None  # paused jobs have no next fire
    assert scheduler.resume_job("j") is True
    assert scheduler.next_run_time("j") is not None
    assert scheduler.pause_job("unknown") is False
    assert scheduler.resume_job("unknown") is False
