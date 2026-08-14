"""The JobEngine: one service facade behind both transports.

Every REST handler and every MCP tool calls the same method here; neither
adapter contains logic of its own.
"""

import contextlib
import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from catalog import LoadResult, load_catalog
from catalog.schema import JobConfig, LocalSource
from config import APP_VERSION, Settings
from engine.locks import LockingRunner, RunRejectedError
from engine.runner import FullScope, Mode
from scheduler import IngestScheduler, jobs_to_run_on_startup
from sources import scan_tree
from state import RunRow, StateStore
from state.models import RunTrigger
from store import QdrantWriter

log = logging.getLogger("engine")

DepProbe = Callable[[], bool]


class UnknownJobError(Exception):
    """The job id is not in the active catalog."""


class JobStillActiveError(Exception):
    """Orphan cleanup was requested for a job that still exists."""


class JobEngine:
    def __init__(
        self,
        settings: Settings,
        state: StateStore,
        writer: QdrantWriter,
        locking: LockingRunner,
        *,
        dep_probes: Mapping[str, DepProbe] | None = None,
        environ: Mapping[str, str] | None = None,
        metrics_hook: Callable[[RunRow], None] | None = None,
    ) -> None:
        self._settings = settings
        self._state = state
        self._writer = writer
        self._locking = locking
        self._dep_probes = dict(dep_probes or {})
        self._environ = environ
        self._metrics_hook = metrics_hook

        self._config_lock = threading.Lock()
        self._jobs: dict[str, JobConfig] = {}
        self._last_load: LoadResult | None = None
        self._config_applied = True
        self._paused: set[str] = set()

        self._run_state_lock = threading.Lock()
        self._aborted_runs: set[str] = set()
        self._queued: dict[str, dict[str, Any]] = {}
        self._run_threads: set[threading.Thread] = set()
        self._shutdown = threading.Event()
        self._poll_thread: threading.Thread | None = None

        self.scheduler = IngestScheduler(settings, execute=self._cron_execute)

    # ── lifecycle ────────────────────────────────────────────────────────────

    def startup(self, *, fire_startup_runs: bool = True) -> None:
        reconciled = self._state.reconcile_interrupted_runs()
        if reconciled:
            log.info("reconciled %d interrupted run(s) from a previous life", reconciled)
        self.reload_config(initial=True)
        self.scheduler.start()
        if fire_startup_runs:
            due = jobs_to_run_on_startup(
                list(self._jobs.values()), self._state, self._settings
            )
            for job in due:
                with contextlib.suppress(RunRejectedError):  # races only
                    self.trigger_run(job.id, "startup")
        if self._settings.jobs_reload_interval > 0:
            self._poll_thread = threading.Thread(
                target=self._poll_config, name="config-poll", daemon=True
            )
            self._poll_thread.start()

    def shutdown(self) -> None:
        self._shutdown.set()
        self.scheduler.shutdown()

    def wait_for_runs(self, grace_seconds: float) -> None:
        """Give running jobs time to stop cooperatively between documents."""
        deadline = time.monotonic() + grace_seconds
        with self._run_state_lock:
            threads = list(self._run_threads)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

    @property
    def is_shutting_down(self) -> bool:
        return self._shutdown.is_set()

    # ── configuration ────────────────────────────────────────────────────────

    def reload_config(self, *, initial: bool = False) -> LoadResult:
        result = load_catalog(self._settings.jobs_file, self._settings, self._environ)
        with self._config_lock:
            # Transactional: a catalog with errors never replaces a working
            # registry. At startup (no previous registry) the valid subset is
            # accepted so one typo cannot zero the whole service.
            apply = result.ok or initial or not self._jobs
            if apply:
                self._jobs = {job.id: job for job in result.jobs}
                self.scheduler.apply_catalog(
                    [job for job in result.jobs if job.id not in self._paused]
                )
            self._last_load = result
            self._config_applied = apply

        orphans = self._state.orphan_summary(set(self._jobs))
        for orphan in orphans:
            log.warning(
                "state tracks job '%s' (collection '%s', %d rows) that is not in "
                "the catalog; see GET /v1/orphans",
                orphan["job_id"],
                orphan["collection"],
                orphan["state_rows"],
            )
        return result

    def _poll_config(self) -> None:
        """mtime+size poll — inotify over bind mounts is unreliable."""
        path = Path(self._settings.jobs_file)
        last_stat: tuple[int, int] | None = self._stat_of(path)
        while not self._shutdown.wait(self._settings.jobs_reload_interval):
            current = self._stat_of(path)
            if current != last_stat:
                last_stat = current
                log.info("jobs.yaml changed on disk; reloading")
                self.reload_config()

    @staticmethod
    def _stat_of(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def config_info(self) -> dict[str, Any]:
        load = self._last_load
        return {
            "path": self._settings.jobs_file,
            "checksum": load.checksum if load else None,
            "loaded_at": load.loaded_at.isoformat() if load else None,
            "valid": bool(load and load.ok),
            "applied": self._config_applied,
            "errors": [
                {"job_id": issue.job_id, "field": issue.field, "message": issue.message}
                for issue in (load.errors if load else [])
            ],
        }

    def config_error(self) -> str | None:
        return self._last_load.config_error if self._last_load else None

    # ── job registry views ───────────────────────────────────────────────────

    def jobs(self) -> list[JobConfig]:
        with self._config_lock:
            return list(self._jobs.values())

    def get_job(self, job_id: str) -> JobConfig:
        with self._config_lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise UnknownJobError(job_id)
        return job

    def is_paused(self, job_id: str) -> bool:
        return job_id in self._paused

    def pause_job(self, job_id: str) -> bool:
        self.get_job(job_id)
        self._paused.add(job_id)
        self.scheduler.pause_job(job_id)
        return True

    def resume_job(self, job_id: str) -> bool:
        self.get_job(job_id)
        self._paused.discard(job_id)
        return self.scheduler.resume_job(job_id)

    def sibling_job_ids(self, job: JobConfig) -> list[str]:
        return [
            other.id
            for other in self.jobs()
            if other.id != job.id
            and other.enabled
            and other.target.collection == job.target.collection
        ]

    def job_summary(self, job: JobConfig) -> dict[str, Any]:
        last_runs = self._state.list_runs(job_id=job.id, limit=1)
        next_run = self.scheduler.next_run_time(job.id)
        return {
            "id": job.id,
            "enabled": job.enabled,
            "paused": self.is_paused(job.id),
            "source": {"type": job.source.type, "label": job.source.label},
            "collection": job.target.collection,
            "mode": job.mode,
            "cron": job.schedule.cron,
            "every": job.schedule.every,
            "next_run_at": next_run.isoformat() if next_run else None,
            "last_run": last_runs[0].as_dict() if last_runs else None,
        }

    def job_detail(self, job: JobConfig) -> dict[str, Any]:
        config = job.model_dump(mode="json", by_alias=True)
        source = config.get("source", {})
        for field_name in type(job.source).secret_fields:
            alias = "pass" if field_name == "password" else field_name
            if source.get(alias) is not None:
                source[alias] = "***"
        next_run = self.scheduler.next_run_time(job.id)
        return {
            "config": config,
            "paused": self.is_paused(job.id),
            "next_run_at": next_run.isoformat() if next_run else None,
            "runs": [run.as_dict() for run in self._state.list_runs(job_id=job.id, limit=10)],
        }

    # ── runs ─────────────────────────────────────────────────────────────────

    def trigger_run(
        self,
        job_id: str,
        trigger: RunTrigger,
        *,
        mode: Mode | None = None,
        full_scope: FullScope | None = None,
        dry_run: bool = False,
        skip_sync: bool = False,
        force: bool = False,
        queue: bool = False,
    ) -> dict[str, Any]:
        """Start a run asynchronously. Raises RunRejectedError on overlap
        (unless queue=True, which enqueues exactly one follow-up run)."""
        job = self.get_job(job_id)
        try:
            job_lock = self._locking.begin(job)
        except RunRejectedError:
            if queue:
                with self._run_state_lock:
                    self._queued.setdefault(
                        job_id,
                        {
                            "mode": mode,
                            "full_scope": full_scope,
                            "dry_run": dry_run,
                            "skip_sync": skip_sync,
                            "force": force,
                        },
                    )
                return {"run_id": None, "queued": True}
            raise

        run_id = str(uuid.uuid4())

        def target() -> None:
            try:
                run = self._locking.execute(
                    job_lock,
                    job,
                    trigger,
                    run_id=run_id,
                    mode=mode,
                    full_scope=full_scope,
                    force=force,
                    dry_run=dry_run,
                    skip_sync=skip_sync,
                    should_abort=lambda: self._abort_requested(run_id),
                    sibling_job_ids=self.sibling_job_ids(job),
                )
                if self._metrics_hook is not None:
                    self._metrics_hook(run)
            finally:
                with self._run_state_lock:
                    self._aborted_runs.discard(run_id)
                    self._run_threads.discard(threading.current_thread())
                    followup = self._queued.pop(job.id, None)
                if followup is not None and not self._shutdown.is_set():
                    with contextlib.suppress(RunRejectedError, UnknownJobError):
                        self.trigger_run(job.id, trigger, **followup)

        thread = threading.Thread(target=target, name=f"run-{job_id}", daemon=True)
        with self._run_state_lock:
            self._run_threads.add(thread)
        thread.start()
        return {"run_id": run_id, "queued": False}

    def run_sync(
        self,
        job_id: str,
        trigger: RunTrigger,
        *,
        mode: Mode | None = None,
        full_scope: FullScope | None = None,
        dry_run: bool = False,
        skip_sync: bool = False,
        force: bool = False,
    ) -> RunRow:
        """Run in the calling thread (cron path and tests)."""
        job = self.get_job(job_id)
        run = self._locking.run(
            job,
            trigger,
            mode=mode,
            full_scope=full_scope,
            force=force,
            dry_run=dry_run,
            skip_sync=skip_sync,
            should_abort=self._shutdown.is_set,
            sibling_job_ids=self.sibling_job_ids(job),
        )
        if self._metrics_hook is not None:
            self._metrics_hook(run)
        return run

    def _cron_execute(self, job: JobConfig) -> None:
        if self._shutdown.is_set() or self.is_paused(job.id):
            return
        try:
            self.run_sync(job.id, "cron")
        except RunRejectedError:
            log.info("[%s] cron firing skipped: job already running", job.id)
        except UnknownJobError:  # pragma: no cover - reload race
            pass

    def _abort_requested(self, run_id: str) -> bool:
        if self._shutdown.is_set():
            return True
        with self._run_state_lock:
            return run_id in self._aborted_runs

    def request_abort(self, run_id: str) -> bool:
        run = self._state.get_run(run_id)
        if run is None or run.status != "running":
            return False
        with self._run_state_lock:
            self._aborted_runs.add(run_id)
        return True

    # ── read models ──────────────────────────────────────────────────────────

    def list_runs(
        self,
        job_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        since: str | None = None,
    ) -> list[RunRow]:
        return self._state.list_runs(job_id=job_id, status=status, limit=limit, since=since)

    def run_detail(self, run_id: str) -> dict[str, Any] | None:
        run = self._state.get_run(run_id)
        if run is None:
            return None
        return {
            "run": run.as_dict(),
            "events": [event.as_dict() for event in self._state.list_events(run_id)],
        }

    def collections(self) -> list[dict[str, Any]]:
        by_collection: dict[str, list[str]] = {}
        for job in self.jobs():
            if job.enabled:
                by_collection.setdefault(job.target.collection, []).append(job.id)
        existing = self._writer.collection_names()
        result = []
        for collection, job_ids in sorted(by_collection.items()):
            entry: dict[str, Any] = {"collection": collection, "jobs": sorted(job_ids)}
            if collection in existing:
                entry["points"] = self._writer.count_points(collection)
                entry["meta"] = self._writer.read_meta(collection)
                entry["indexes"] = sorted(self._writer.payload_index_fields(collection))
            else:
                entry["points"] = 0
                entry["meta"] = None
                entry["indexes"] = []
            result.append(entry)
        return result

    def orphans(self) -> list[dict[str, Any]]:
        known = {job.id for job in self.jobs()}
        existing = self._writer.collection_names()
        result = []
        for orphan in self._state.orphan_summary(known):
            points = 0
            if orphan["collection"] in existing:
                points = self._writer.count_points(orphan["collection"], orphan["job_id"])
            result.append({**orphan, "points": points})
        return result

    def delete_orphan(self, job_id: str) -> dict[str, int]:
        with self._config_lock:
            if job_id in self._jobs:
                raise JobStillActiveError(job_id)
        collections = {
            orphan["collection"]
            for orphan in self._state.orphan_summary(set())
            if orphan["job_id"] == job_id
        }
        existing = self._writer.collection_names()
        deleted_points = 0
        for collection in collections:
            if collection in existing:
                deleted_points += self._writer.count_points(collection, job_id)
                self._writer.delete_job_points(collection, job_id)
        deleted_rows = self._state.delete_documents_for_job(job_id)
        return {"deleted_points": deleted_points, "deleted_rows": deleted_rows}

    def preview(self, job_id: str, limit: int = 50) -> list[dict[str, Any]]:
        job = self.get_job(job_id)
        scan_root = (
            Path(job.source.path)
            if isinstance(job.source, LocalSource)
            else Path(self._settings.cache_dir) / job.source.label
        )
        files = scan_tree(scan_root, job.filters)
        return [
            {
                "rel_path": file.rel_path,
                "source": job.source_uri(file.rel_path),
                "size": file.size,
            }
            for file in files[:limit]
        ]

    # ── health ───────────────────────────────────────────────────────────────

    def health(self) -> dict[str, Any]:
        deps = {name: probe() for name, probe in self._dep_probes.items()}
        config_error = self.config_error()
        degraded = bool(config_error) or not all(deps.values())
        return {
            "status": "degraded" if degraded else "ok",
            "version": APP_VERSION,
            "jobs_loaded": len(self.jobs()),
            "config_error": config_error,
            "deps": deps,
        }
