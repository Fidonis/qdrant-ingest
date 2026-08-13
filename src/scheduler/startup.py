"""run_on_startup: catch up on windows missed while the container was down."""

import logging
from datetime import UTC, datetime

from catalog.schema import JobConfig
from config import Settings
from scheduler.aps import build_trigger
from state import StateStore

log = logging.getLogger("scheduler")

# A run counts as missed when the last success is older than this multiple of
# the nominal firing interval.
_MISSED_FACTOR = 1.5


def nominal_interval_seconds(
    job: JobConfig, settings: Settings, now: datetime | None = None
) -> float | None:
    """Seconds between two consecutive firings of the job's trigger."""
    if job.schedule.every_seconds is not None:
        return float(job.schedule.every_seconds)
    trigger = build_trigger(job, settings)
    if trigger is None:
        return None
    trigger.jitter = None  # jitter would distort the nominal interval
    moment = now or datetime.now(tz=UTC)
    first = trigger.get_next_fire_time(None, moment)
    if first is None:
        return None
    second = trigger.get_next_fire_time(first, first)
    if second is None:
        return None
    return float((second - first).total_seconds())


def jobs_to_run_on_startup(
    jobs: list[JobConfig],
    state: StateStore,
    settings: Settings,
    now: datetime | None = None,
) -> list[JobConfig]:
    """Jobs that should fire once at startup, per their run_on_startup policy."""
    moment = now or datetime.now(tz=UTC)
    due: list[JobConfig] = []
    for job in jobs:
        if not job.enabled or job.schedule.is_manual_only:
            continue
        policy = job.schedule.run_on_startup
        if policy == "never":
            continue
        if policy == "always":
            due.append(job)
            continue
        interval = nominal_interval_seconds(job, settings, moment)
        if interval is None:
            continue
        last = state.last_successful_run(job.id)
        if last is None or last.finished_at is None:
            due.append(job)
            continue
        finished = datetime.fromisoformat(last.finished_at)
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=UTC)
        if (moment - finished).total_seconds() > _MISSED_FACTOR * interval:
            log.info(
                "[%s] last success at %s exceeds %.1fx the nominal interval; "
                "firing once at startup",
                job.id,
                last.finished_at,
                _MISSED_FACTOR,
            )
            due.append(job)
    return due
