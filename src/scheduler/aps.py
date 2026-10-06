"""APScheduler 3.x wiring.

Memory jobstore on purpose: jobs.yaml is the declarative truth, and a
persistent jobstore would be a second one — a deleted job could resurrect
from disk after a restart. Run *history* is persisted (the runs table); the
scheduler state is not.
"""

import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from catalog.schema import JobConfig, ScheduleConfig
from config import Settings

log = logging.getLogger("scheduler")

ExecuteFn = Callable[[JobConfig], None]

# A local copy of jobs.yaml's `every:` grammar, so a prospective schedule can
# be previewed (below) without building a whole ScheduleConfig.
_EVERY_RE = re.compile(r"^(\d+)(s|m|h|d)$")
_EVERY_FACTORS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def build_trigger(job: JobConfig, settings: Settings) -> CronTrigger | IntervalTrigger | None:
    schedule = job.schedule
    timezone = schedule.timezone or settings.timezone
    if schedule.cron is not None:
        trigger = CronTrigger.from_crontab(schedule.cron, timezone=timezone)
        trigger.jitter = schedule.jitter_seconds or None
        return trigger
    if schedule.every_seconds is not None:
        return IntervalTrigger(
            seconds=schedule.every_seconds,
            timezone=timezone,
            jitter=schedule.jitter_seconds or None,
        )
    return None


def preview_fire_times(
    *,
    cron: str | None,
    every: str | None,
    timezone: str,
    count: int = 3,
    now: datetime | None = None,
) -> list[datetime]:
    """The next ``count`` firings of a prospective schedule.

    Builds a throwaway trigger from the same ``cron`` / ``every`` a job would
    carry and walks it forward. Nothing is added to the live scheduler. Raises
    ``ValueError`` for an expression APScheduler cannot parse; the caller turns
    that into a failed preview rather than a 500.
    """
    trigger: CronTrigger | IntervalTrigger
    if cron:
        trigger = CronTrigger.from_crontab(cron, timezone=timezone)
    elif every:
        match = _EVERY_RE.match(every)
        if match is None:
            raise ValueError("every must look like '30s', '15m', '4h', or '1d'")
        seconds = int(match.group(1)) * _EVERY_FACTORS[match.group(2)]
        trigger = IntervalTrigger(seconds=seconds, timezone=timezone)
    else:
        return []

    current = now or datetime.now(trigger.timezone)
    fired: list[datetime] = []
    previous: datetime | None = None
    while len(fired) < count:
        upcoming = trigger.get_next_fire_time(previous, current)
        if upcoming is None:
            break
        fired.append(upcoming)
        previous = upcoming
        current = upcoming + timedelta(seconds=1)
    return fired


class IngestScheduler:
    """Owns the BackgroundScheduler and diffs the catalog into it."""

    def __init__(self, settings: Settings, execute: ExecuteFn) -> None:
        self._settings = settings
        self._execute = execute
        # The schedule each live job was added with. A job is added again only when its
        # schedule differs: an IntervalTrigger starts counting when it is created, so
        # re-adding an unchanged job would push its next run back by a whole interval.
        self._applied: dict[str, ScheduleConfig] = {}
        self._scheduler = BackgroundScheduler(
            jobstores={"default": MemoryJobStore()},
            executors={
                "default": ThreadPoolExecutor(max_workers=settings.max_concurrent_jobs)
            },
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": settings.misfire_grace,
            },
            timezone=settings.timezone,
        )

    def start(self) -> None:
        self._scheduler.start()

    def shutdown(self) -> None:
        # wait=False: no new firings; running jobs finish cooperatively via
        # the engine's abort flag, not by blocking here.
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    def apply_catalog(self, jobs: list[JobConfig]) -> None:
        """Diff the declarative catalog against the live scheduler.

        A currently running job is never interrupted; a replaced definition
        takes effect at its next firing. A job whose schedule did not change keeps
        its trigger, so a reload (and any edit of another job) does not move its
        next run, and a paused job stays paused.
        """
        desired: dict[str, JobConfig] = {}
        for job in jobs:
            if job.enabled and not job.schedule.is_manual_only:
                desired[job.id] = job

        existing_ids = {aps_job.id for aps_job in self._scheduler.get_jobs()}
        for job_id in existing_ids - set(desired):
            self._scheduler.remove_job(job_id)
            log.info("unscheduled job '%s'", job_id)
        self._applied = {
            job_id: schedule for job_id, schedule in self._applied.items() if job_id in desired
        }

        for job_id, job in desired.items():
            live: Any = self._scheduler.get_job(job_id)
            if live is not None and self._applied.get(job_id) == job.schedule:
                live.modify(args=[job])
                continue
            trigger = build_trigger(job, self._settings)
            if trigger is None:  # pragma: no cover - filtered above
                continue
            self._scheduler.add_job(
                self._execute,
                trigger=trigger,
                args=[job],
                id=job_id,
                name=job_id,
                replace_existing=True,
                misfire_grace_time=job.schedule.misfire_grace_seconds,
            )
            self._applied[job_id] = job.schedule

    def scheduled_ids(self) -> set[str]:
        return {aps_job.id for aps_job in self._scheduler.get_jobs()}

    def next_run_time(self, job_id: str) -> datetime | None:
        aps_job: Any = self._scheduler.get_job(job_id)
        return getattr(aps_job, "next_run_time", None) if aps_job else None

    def pause_job(self, job_id: str) -> bool:
        if self._scheduler.get_job(job_id) is None:
            return False
        self._scheduler.pause_job(job_id)
        return True

    def resume_job(self, job_id: str) -> bool:
        if self._scheduler.get_job(job_id) is None:
            return False
        self._scheduler.resume_job(job_id)
        return True
