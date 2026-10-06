"""The JobEngine: one service facade behind both transports.

Every REST handler and every MCP tool calls the same method here; neither
adapter contains logic of its own.
"""

import contextlib
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from catalog import LoadResult, load_catalog, load_catalog_bytes
from catalog.schema import JobConfig, LocalSource
from catalog.secret_store import SecretStore
from config import APP_VERSION, Settings
from connections.loader import ResolvedConnection
from connections.registry import ConnectionRegistry, UnknownConnectionError
from engine.locks import LockingRunner, RunRejectedError
from engine.runner import FullScope, Mode
from scheduler import IngestScheduler, jobs_to_run_on_startup, preview_fire_times
from sources import scan_tree
from state import RunRow, StateStore, now_iso
from state.models import DOCUMENT_STATUSES, RunTrigger
from store import QdrantWriter

log = logging.getLogger("engine")

DepProbe = Callable[[], bool]

# What this build can do beyond the original REST surface, announced in /health so a
# client (the management panel) can adapt to an older ingester without comparing version
# numbers. Each entry is added together with the feature it names.
FEATURES: tuple[str, ...] = ("run_progress", "documents", "validate")

DOCUMENT_ORDERS = ("path", "recent")

# The dependency probes reach out to Qdrant, the embeddings endpoint, and
# Tika. They are refreshed on this interval by a background thread and never
# run on the /health request path: an unreachable dependency takes seconds to
# time out, which would push /health past its own healthcheck timeout and mark
# a perfectly serving container unhealthy.
_DEPS_PROBE_INTERVAL = 15.0


class UnknownJobError(Exception):
    """The job id is not in the active catalog."""


class JobStillActiveError(Exception):
    """Orphan cleanup was requested for a job that still exists."""


class JobEngine:
    def __init__(
        self,
        settings: Settings,
        state: StateStore,
        registry: ConnectionRegistry,
        locking: LockingRunner,
        *,
        dep_probes: Mapping[str, DepProbe] | None = None,
        environ: Mapping[str, str] | None = None,
        secret_store: SecretStore | None = None,
        metrics_hook: Callable[[RunRow], None] | None = None,
    ) -> None:
        self._settings = settings
        self._state = state
        self._registry = registry
        self._locking = locking
        self._dep_probes = dict(dep_probes or {})
        # The Qdrant probe is the engine's own: it depends on which connections
        # the enabled jobs reference, which only the engine knows. A caller that
        # passes its own "qdrant" entry (the tests do) still wins.
        self._dep_probes.setdefault("qdrant", self._probe_qdrant)
        self._environ = environ
        self._secret_store = secret_store
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

        self._deps_lock = threading.Lock()
        self._deps: dict[str, bool] = dict.fromkeys(self._dep_probes, False)
        self._deps_checked_at: str | None = None
        self._deps_thread: threading.Thread | None = None

        self.scheduler = IngestScheduler(settings, execute=self._cron_execute)

    @property
    def environ(self) -> Mapping[str, str]:
        """What ``${env:QI_SECRET_...}`` resolves against: the process environment, plus the
        secret store when this engine has one."""
        return os.environ if self._environ is None else self._environ

    # ── lifecycle ────────────────────────────────────────────────────────────

    def startup(self, *, fire_startup_runs: bool = True) -> None:
        reconciled = self._state.reconcile_interrupted_runs()
        if reconciled:
            log.info("reconciled %d interrupted run(s) from a previous life", reconciled)
        self._registry.reload(initial=True)
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
        if self._dep_probes:
            self._deps_thread = threading.Thread(
                target=self._poll_deps, name="deps-probe", daemon=True
            )
            self._deps_thread.start()

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

    def reload_connections(self, *, initial: bool = False) -> None:
        """Re-read connections.yaml, then re-validate the job catalog against it."""
        self._registry.reload(initial=initial)
        self.reload_config()

    def reload_config(self, *, initial: bool = False) -> LoadResult:
        result = load_catalog(
            self._settings.jobs_file,
            self._settings,
            self._environ,
            known_connections=self._registry.names(),
        )
        with self._config_lock:
            # Transactional: a catalog with errors never replaces a working
            # registry. At startup (no previous registry) the valid subset is
            # accepted so one typo cannot zero the whole service.
            apply = result.ok or initial or not self._jobs
            if apply:
                self._jobs = {job.id: job for job in result.jobs}
                # A paused job is applied like any other and paused again: leaving it
                # out would remove it from the scheduler, and resume could not bring it back.
                self._paused &= set(self._jobs)
                self.scheduler.apply_catalog(list(result.jobs))
                for paused_id in self._paused:
                    self.scheduler.pause_job(paused_id)
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
        """mtime+size poll — inotify over bind mounts is unreliable.

        Watches both the job catalog and connections.yaml; a change to either
        re-reads the connections and re-validates the catalog against them.
        """
        jobs_path = Path(self._settings.jobs_file)
        conn_path = Path(self._settings.connections_file)
        secrets_path = Path(self._settings.secrets_file)
        last_jobs = self._stat_of(jobs_path)
        last_conn = self._stat_of(conn_path)
        last_secrets = self._stat_of(secrets_path)
        while not self._shutdown.wait(self._settings.jobs_reload_interval):
            current_conn = self._stat_of(conn_path)
            if current_conn != last_conn:
                last_conn = current_conn
                log.info("connections.yaml changed on disk; reloading")
                self.reload_connections()
                last_jobs = self._stat_of(jobs_path)
                continue
            current_secrets = self._stat_of(secrets_path)
            if current_secrets != last_secrets:
                # Whether a job is valid depends on its secrets existing, so a secret that
                # appears (or breaks) is a catalog change even though jobs.yaml is the same.
                last_secrets = current_secrets
                log.info("secrets.yaml changed on disk; reloading")
                self.reload_config()
                last_jobs = self._stat_of(jobs_path)
                continue
            current_jobs = self._stat_of(jobs_path)
            if current_jobs != last_jobs:
                last_jobs = current_jobs
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
            "connections": self._registry.config_info(),
        }

    def validate_catalog(self, raw: str) -> dict[str, Any]:
        """Would this text load as the catalog? Answered without writing or applying it.

        The same loader as a reload, against the connections and secrets this process
        has right now, so the answer is the one a reload of that file would give.
        """
        result = load_catalog_bytes(
            raw, self._settings, self._environ, known_connections=self._registry.names()
        )
        return {
            "ok": result.ok,
            "errors": [
                {"job_id": issue.job_id, "field": issue.field, "message": issue.message}
                for issue in result.errors
            ],
            "jobs": len(result.jobs),
        }

    def config_error(self) -> str | None:
        catalog_error = self._last_load.config_error if self._last_load else None
        connections_error = self._registry.error()
        if connections_error is not None:
            return f"connections.yaml: {connections_error}"
        secrets_error = self._secret_store.problem() if self._secret_store else None
        if secrets_error is not None:
            return f"secrets.yaml: {secrets_error}"
        return catalog_error

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

    # ── connections ──────────────────────────────────────────────────────────

    def connection_names(self) -> set[str]:
        return self._registry.names()

    def connection(self, name: str) -> ResolvedConnection | None:
        return self._registry.get(name)

    def jobs_using_connection(self, name: str) -> list[str]:
        return [job.id for job in self.jobs() if job.target.connection == name]

    def connections_view(self) -> list[dict[str, Any]]:
        """One row per resolved connection for the interface's list page."""
        rows: list[dict[str, Any]] = []
        for connection in sorted(self._registry.all(), key=lambda c: c.name):
            rows.append(
                {
                    "name": connection.name,
                    "url": connection.url,
                    "has_key": connection.api_key is not None,
                    "used_by": self.jobs_using_connection(connection.name),
                }
            )
        return rows

    def _probe_qdrant(self) -> bool:
        referenced = {job.target.connection for job in self.jobs() if job.enabled}
        return self._registry.ping_all(referenced)

    def job_summary(self, job: JobConfig) -> dict[str, Any]:
        last_runs = self._state.list_runs(job_id=job.id, limit=1)
        next_run = self.scheduler.next_run_time(job.id)
        documents, chunks = self._state.document_totals(job.id)
        return {
            "id": job.id,
            "enabled": job.enabled,
            "paused": self.is_paused(job.id),
            "source": {"type": job.source.type, "label": job.source.label},
            "collection": job.target.collection,
            "connection": job.target.connection,
            "mode": job.mode,
            "cron": job.schedule.cron,
            "every": job.schedule.every,
            "next_run_at": next_run.isoformat() if next_run else None,
            "last_run": last_runs[0].as_dict() if last_runs else None,
            "documents": {"total": documents, "chunks": chunks},
        }

    def preview_schedule(
        self, cron: str | None, every: str | None, timezone: str | None = None
    ) -> dict[str, Any]:
        """Describe a prospective schedule for the job editor: the next few
        firings, or the reason the expression will not load. A read-only probe
        -- it builds a throwaway trigger and never touches the live scheduler.
        """
        resolved_tz = timezone or self._settings.timezone
        if cron and every:
            return {
                "ok": False,
                "error": "A schedule takes either a cron expression or an interval, not both.",
                "times": [],
            }
        try:
            fired = preview_fire_times(
                cron=cron, every=every, timezone=resolved_tz, count=3
            )
        except Exception as exc:  # noqa: BLE001 - any parse failure is just a failed preview
            return {"ok": False, "error": str(exc), "times": []}
        return {
            "ok": True,
            "error": None,
            "times": [moment.isoformat() for moment in fired],
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
        delete_vanished: bool = True,
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
                            "delete_vanished": delete_vanished,
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
                    delete_vanished=delete_vanished,
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

    def documents(
        self,
        job_id: str,
        *,
        status: str | None = None,
        query: str | None = None,
        run_id: str | None = None,
        order: str = "path",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """The documents a job tracks, one page of them, with the counts to build a filter.

        This is the state the ingester keeps to decide what to do next time, so it says
        what happened to each file: indexed, skipped (and why) or failed (and why).
        ``counts`` is over the whole job, not over the filter, so a filter bar can show
        how many there are of each status. ``run_id`` matches the run that last touched
        the document; a file the run found unchanged is not touched and so not listed.
        """
        self.get_job(job_id)
        if status is not None and status not in DOCUMENT_STATUSES:
            raise ValueError(f"unknown document status {status!r}")
        if order not in DOCUMENT_ORDERS:
            raise ValueError(f"unknown order {order!r}")
        rows = self._state.list_documents_page(
            job_id,
            status=status,
            query=query,
            run_id=run_id,
            order=order,
            limit=limit,
            offset=offset,
        )
        return {
            "total": self._state.count_documents_filtered(
                job_id, status=status, query=query, run_id=run_id
            ),
            "counts": self._state.count_documents_by_status(job_id),
            "items": [
                {
                    "source": row.source,
                    "rel_path": row.rel_path,
                    "status": row.status,
                    "last_error": row.last_error,
                    "indexed_at": row.indexed_at,
                    "chunk_count": row.chunk_count,
                    "size": row.size,
                    "media_type": row.media_type,
                    "last_run_id": row.last_run_id,
                }
                for row in rows
            ],
        }

    def _writer_for_collection(self, collection: str) -> QdrantWriter | None:
        """The writer of whichever connection serves this collection.

        One connection per collection is enforced at load time, so any job on
        the collection names the right one. Returns None when the connection
        did not resolve.
        """
        for job in self.jobs():
            if job.target.collection == collection:
                try:
                    return self._registry.writer(job.target.connection)
                except UnknownConnectionError:
                    return None
        return None

    def collections(self) -> list[dict[str, Any]]:
        by_collection: dict[str, list[str]] = {}
        for job in self.jobs():
            if job.enabled:
                by_collection.setdefault(job.target.collection, []).append(job.id)
        result = []
        for collection, job_ids in sorted(by_collection.items()):
            entry: dict[str, Any] = {"collection": collection, "jobs": sorted(job_ids)}
            writer = self._writer_for_collection(collection)
            if writer is not None and collection in writer.collection_names():
                entry["points"] = writer.count_points(collection)
                entry["meta"] = writer.read_meta(collection)
                entry["indexes"] = sorted(writer.payload_index_fields(collection))
            else:
                entry["points"] = 0
                entry["meta"] = None
                entry["indexes"] = []
            result.append(entry)
        return result

    def orphans(self) -> list[dict[str, Any]]:
        known = {job.id for job in self.jobs()}
        # An orphan's job is gone from the catalog, so its connection is
        # unknown -- count its points across every connection that resolved.
        writers = [self._registry.writer(name) for name in sorted(self._registry.names())]
        result = []
        for orphan in self._state.orphan_summary(known):
            points = 0
            for writer in writers:
                if orphan["collection"] in writer.collection_names():
                    points += writer.count_points(orphan["collection"], orphan["job_id"])
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
        writers = [self._registry.writer(name) for name in sorted(self._registry.names())]
        deleted_points = 0
        for collection in collections:
            for writer in writers:
                if collection in writer.collection_names():
                    deleted_points += writer.count_points(collection, job_id)
                    writer.delete_job_points(collection, job_id)
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

    def refresh_deps(self) -> dict[str, bool]:
        """Probe every dependency once and cache the outcome.

        Called by the background thread, and directly by tests that want a
        deterministic snapshot.
        """
        probed: dict[str, bool] = {}
        for name, probe in self._dep_probes.items():
            try:
                probed[name] = bool(probe())
            except Exception:
                # Any failure to answer is indistinguishable from "down".
                probed[name] = False
        with self._deps_lock:
            self._deps = probed
            self._deps_checked_at = now_iso()
        return probed

    def _poll_deps(self) -> None:
        while not self._shutdown.is_set():
            self.refresh_deps()
            self._shutdown.wait(_DEPS_PROBE_INTERVAL)

    def health(self) -> dict[str, Any]:
        """Cheap by construction: reads the cached probe results, no I/O."""
        with self._deps_lock:
            deps = dict(self._deps)
            checked_at = self._deps_checked_at
        config_error = self.config_error()
        # Until the first probe lands, dependencies are unknown rather than
        # down — reporting them as down would degrade every fresh start.
        deps_down = checked_at is not None and not all(deps.values())
        return {
            "status": "degraded" if bool(config_error) or deps_down else "ok",
            "version": APP_VERSION,
            "jobs_loaded": len(self.jobs()),
            "config_error": config_error,
            "deps": deps,
            "deps_checked_at": checked_at,
            "features": [*FEATURES, *(["secret_store"] if self._secret_store else [])],
        }
