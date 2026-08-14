"""Run serialisation.

Three overlap layers protect runs, and they do not replace each other:
APScheduler's ``max_instances=1``/``coalesce`` only covers cron firings; the
non-blocking per-job lock covers manual triggers that bypass the scheduler;
and the per-collection reader/writer lock is a *correctness* requirement for
the generation sweep — points written by a concurrent upsert would carry a
foreign run id and be deleted by a full run's sweep. Full runs therefore take
the collection lock exclusively, everything else shared.
"""

import threading
import time
import uuid
from collections.abc import Sequence

from catalog.schema import JobConfig
from engine.runner import FullScope, JobRunner, Mode, ShouldAbort
from state import RunRow, StateStore, now_iso
from state.models import RunTrigger


class RunRejectedError(Exception):
    """A concurrent run of the same job is already active."""

    def __init__(self, job_id: str, active_run_id: str | None) -> None:
        super().__init__(f"job '{job_id}' is already running")
        self.job_id = job_id
        self.active_run_id = active_run_id


class _ReadWriteLock:
    """Minimal timeout-capable reader/writer lock."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._readers = 0
        self._writer = False

    def acquire(self, *, exclusive: bool, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._cond:
            if exclusive:
                acquired = self._cond.wait_for(
                    lambda: not self._writer and self._readers == 0,
                    timeout=max(0.0, deadline - time.monotonic()),
                )
                if acquired:
                    self._writer = True
                return acquired
            acquired = self._cond.wait_for(
                lambda: not self._writer,
                timeout=max(0.0, deadline - time.monotonic()),
            )
            if acquired:
                self._readers += 1
            return acquired

    def release(self, *, exclusive: bool) -> None:
        with self._cond:
            if exclusive:
                self._writer = False
            else:
                self._readers = max(0, self._readers - 1)
            self._cond.notify_all()


class LockingRunner:
    """Wraps the JobRunner with the job lock and the collection RW lock."""

    def __init__(
        self, runner: JobRunner, state: StateStore, lock_timeout: float
    ) -> None:
        self._runner = runner
        self._state = state
        self._lock_timeout = lock_timeout
        self._registry_lock = threading.Lock()
        self._job_locks: dict[str, threading.Lock] = {}
        self._collection_locks: dict[str, _ReadWriteLock] = {}

    def _job_lock(self, job_id: str) -> threading.Lock:
        with self._registry_lock:
            return self._job_locks.setdefault(job_id, threading.Lock())

    def _collection_lock(self, collection: str) -> _ReadWriteLock:
        with self._registry_lock:
            return self._collection_locks.setdefault(collection, _ReadWriteLock())

    def active_run_id(self, job_id: str) -> str | None:
        running = self._state.list_runs(job_id=job_id, status="running", limit=1)
        return running[0].run_id if running else None

    def _record_aborted_lock(
        self, job: JobConfig, trigger: RunTrigger, mode: Mode
    ) -> RunRow:
        run = RunRow(
            run_id=str(uuid.uuid4()),
            job_id=job.id,
            mode=mode,
            trigger=trigger,
            started_at=now_iso(),
            finished_at=now_iso(),
            status="aborted_lock",
            error=(
                f"collection '{job.target.collection}' stayed locked for "
                f"{self._lock_timeout:.0f}s; coalescing lets the next tick retry"
            ),
        )
        self._state.create_run(run)
        return run

    def run(
        self,
        job: JobConfig,
        trigger: RunTrigger,
        *,
        mode: Mode | None = None,
        full_scope: FullScope | None = None,
        force: bool = False,
        dry_run: bool = False,
        skip_sync: bool = False,
        should_abort: ShouldAbort | None = None,
        sibling_job_ids: Sequence[str] = (),
    ) -> RunRow:
        effective_mode: Mode = mode or job.mode
        job_lock = self._job_lock(job.id)
        if not job_lock.acquire(blocking=False):
            raise RunRejectedError(job.id, self.active_run_id(job.id))
        try:
            collection_lock = self._collection_lock(job.target.collection)
            exclusive = effective_mode == "full"
            if not collection_lock.acquire(
                exclusive=exclusive, timeout=self._lock_timeout
            ):
                return self._record_aborted_lock(job, trigger, effective_mode)
            try:
                return self._runner.run_job(
                    job,
                    trigger,
                    mode=mode,
                    full_scope=full_scope,
                    force=force,
                    dry_run=dry_run,
                    skip_sync=skip_sync,
                    should_abort=should_abort,
                    sibling_job_ids=sibling_job_ids,
                )
            finally:
                collection_lock.release(exclusive=exclusive)
        finally:
            job_lock.release()
