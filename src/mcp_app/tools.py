"""The MCP tool surface.

Deliberately non-destructive: no orphan cleanup, no collection deletion, no
run cancellation. An assistant should be able to *trigger* a reindex, never
to destroy an index. ``trigger_reindex`` may additionally only pick a mode
that is no more destructive than the configured one — see
:func:`resolve_trigger_mode`.
"""

import logging
from typing import Any, Literal

from fastmcp import FastMCP

from catalog.schema import JobConfig
from engine.locks import RunRejectedError
from engine.runner import Mode
from engine.service import JobEngine, UnknownJobError

logger = logging.getLogger("mcp.tools")

RequestedMode = Literal["full", "append", "upsert"]

# How destructive a mode is. A tool caller may never move *up* this ladder
# beyond what the job itself is configured for.
_DESTRUCTIVENESS: dict[str, int] = {"append": 0, "upsert": 1, "full": 2}


class ModeNotAllowedError(Exception):
    """The requested mode is more destructive than the job permits."""


def resolve_trigger_mode(job: JobConfig, requested: RequestedMode | None) -> Mode:
    """Which mode an MCP-triggered run may actually use.

    - ``append`` is always allowed (it never updates and never deletes).
    - ``upsert`` requires the job to be configured as ``upsert`` or ``full``.
    - ``full`` additionally requires ``mcp_allow_full: true``, and is never
      granted for jobs whose full scope is the whole collection.
    """
    if requested is None:
        # No explicit request: run what the job is configured for, unless
        # that is a full run the operator has not opened up.
        if job.mode == "full" and not _full_allowed(job):
            return "upsert"
        return job.mode
    if requested == "full":
        if not _full_allowed(job):
            raise ModeNotAllowedError(
                f"job '{job.id}' does not allow full reindexing over MCP "
                "(set mcp_allow_full: true, and full_scope must not be 'collection')"
            )
        return "full"
    if _DESTRUCTIVENESS[requested] > _DESTRUCTIVENESS[job.mode]:
        raise ModeNotAllowedError(
            f"job '{job.id}' is configured as '{job.mode}'; "
            f"'{requested}' would be more destructive"
        )
    return requested


def _full_allowed(job: JobConfig) -> bool:
    return job.mcp_allow_full and job.full_scope != "collection"


def register_tools(mcp: FastMCP, engine: JobEngine) -> None:
    """Register every tool against the shared JobEngine."""

    def _job(job_id: str) -> JobConfig:
        try:
            return engine.get_job(job_id)
        except UnknownJobError as exc:
            raise ValueError(f"unknown job '{job_id}'") from exc

    @mcp.tool
    def list_ingest_jobs() -> dict[str, Any]:
        """List the configured ingestion jobs with their schedule and last run."""
        jobs = [engine.job_summary(job) for job in engine.jobs()]
        return {"jobs": jobs, "count": len(jobs)}

    @mcp.tool
    def get_ingest_job(job_id: str) -> dict[str, Any]:
        """Return one job's resolved configuration (secrets redacted) and recent runs.

        Args:
            job_id: the id of the job as listed by list_ingest_jobs.
        """
        return engine.job_detail(_job(job_id))

    @mcp.tool
    def trigger_reindex(job_id: str, mode: RequestedMode | None = None) -> dict[str, Any]:
        """Start an ingestion run for a job.

        Args:
            job_id: the job to run.
            mode: optional override. 'append' is always permitted; 'upsert'
                requires the job to be an upsert or full job; 'full' requires
                the job to opt in via mcp_allow_full.
        """
        job = _job(job_id)
        try:
            effective = resolve_trigger_mode(job, mode)
        except ModeNotAllowedError as exc:
            logger.warning("MCP.deny trigger_reindex job=%s mode=%s: %s", job_id, mode, exc)
            return {"status": "mode_not_allowed", "job_id": job_id, "detail": str(exc)}
        try:
            result = engine.trigger_run(job_id, "manual_mcp", mode=effective)
        except RunRejectedError as exc:
            return {
                "status": "already_running",
                "job_id": job_id,
                "run_id": exc.active_run_id,
            }
        return {
            "status": "started",
            "job_id": job_id,
            "mode": effective,
            "run_id": result["run_id"],
        }

    @mcp.tool
    def get_ingest_status(job_id: str | None = None) -> dict[str, Any]:
        """Return the service health plus per-job status.

        Args:
            job_id: optional; restrict the report to a single job.
        """
        jobs = [_job(job_id)] if job_id else engine.jobs()
        return {
            "health": engine.health(),
            "jobs": [engine.job_summary(job) for job in jobs],
        }

    @mcp.tool
    def list_ingest_runs(job_id: str | None = None, limit: int = 10) -> dict[str, Any]:
        """List recent ingestion runs, newest first.

        Args:
            job_id: optional; restrict to a single job.
            limit: how many runs to return (1-100).
        """
        bounded = max(1, min(limit, 100))
        runs = engine.list_runs(job_id=job_id, limit=bounded)
        return {"runs": [run.as_dict() for run in runs], "count": len(runs)}

    @mcp.tool
    def get_ingest_run(run_id: str) -> dict[str, Any]:
        """Return one run with its counters and event log.

        Args:
            run_id: the run id returned by trigger_reindex or list_ingest_runs.
        """
        detail = engine.run_detail(run_id)
        if detail is None:
            raise ValueError(f"unknown run '{run_id}'")
        return detail

    @mcp.tool
    def list_ingest_collections() -> dict[str, Any]:
        """List the target collections with point counts and embedding metadata."""
        collections = engine.collections()
        return {"collections": collections, "count": len(collections)}

    @mcp.tool
    def reload_ingest_config() -> dict[str, Any]:
        """Re-read jobs.yaml and report validation errors without restarting."""
        engine.reload_config()
        return engine.config_info()
