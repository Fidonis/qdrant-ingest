"""REST routes — thin adapters over the JobEngine, no logic of their own."""

import json
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response

from api.auth import check_bearer
from api.metrics import Metrics
from api.models import RunRequest
from config import APP_NAME, APP_VERSION, Settings
from engine.locks import RunRejectedError
from engine.service import JobEngine, JobStillActiveError, UnknownJobError


def create_app(settings: Settings, engine: JobEngine, metrics: Metrics) -> FastAPI:
    app = FastAPI(
        title=APP_NAME,
        version=APP_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def require_token(request: Request) -> None:
        check_bearer(request, settings)

    v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])

    # ── free surface ─────────────────────────────────────────────────────────

    @app.get("/health")
    async def health() -> Response:
        # Always 200 while the process can serve: a degraded container must
        # not be restart-looped by its healthcheck.
        return Response(
            content=json.dumps(engine.health()),
            media_type="application/json",
            status_code=200,
        )

    @app.get("/metrics")
    async def metrics_endpoint(request: Request) -> Response:
        if not settings.metrics_enabled:
            raise HTTPException(status_code=404, detail="metrics disabled")
        if settings.metrics_auth:
            check_bearer(request, settings)
        metrics.jobs_loaded.set(len(engine.jobs()))
        return Response(content=metrics.render(), media_type="text/plain; version=0.0.4")

    # ── jobs ─────────────────────────────────────────────────────────────────

    def _job_or_404(job_id: str) -> Any:
        try:
            return engine.get_job(job_id)
        except UnknownJobError as exc:
            raise HTTPException(status_code=404, detail=f"unknown job '{job_id}'") from exc

    @v1.get("/jobs")
    async def list_jobs() -> list[dict[str, Any]]:
        return [engine.job_summary(job) for job in engine.jobs()]

    @v1.get("/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        job = _job_or_404(job_id)
        return engine.job_detail(job)

    @v1.post("/jobs/{job_id}/run", status_code=202)
    async def run_job(job_id: str, body: RunRequest | None = None) -> dict[str, Any]:
        _job_or_404(job_id)
        request = body or RunRequest()
        try:
            return engine.trigger_run(
                job_id,
                "manual_rest",
                mode=request.mode,
                full_scope=request.full_scope,
                dry_run=request.dry_run,
                skip_sync=request.skip_sync,
                force=request.force,
                queue=request.queue,
            )
        except RunRejectedError as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "already_running", "run_id": exc.active_run_id},
            ) from exc

    @v1.post("/jobs/{job_id}/pause")
    async def pause_job(job_id: str) -> dict[str, Any]:
        _job_or_404(job_id)
        engine.pause_job(job_id)
        return {"enabled": False}

    @v1.post("/jobs/{job_id}/resume")
    async def resume_job(job_id: str) -> dict[str, Any]:
        _job_or_404(job_id)
        engine.resume_job(job_id)
        return {"enabled": True}

    @v1.get("/jobs/{job_id}/preview")
    async def preview_job(
        job_id: str, limit: int = Query(default=50, ge=1, le=1000)
    ) -> dict[str, Any]:
        _job_or_404(job_id)
        entries = engine.preview(job_id, limit)
        return {"files": entries, "count": len(entries)}

    # ── runs ─────────────────────────────────────────────────────────────────

    @v1.get("/runs")
    async def list_runs(
        job_id: str | None = None,
        status: str | None = None,
        limit: int = Query(default=50, ge=1, le=1000),
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            run.as_dict()
            for run in engine.list_runs(job_id=job_id, status=status, limit=limit, since=since)
        ]

    @v1.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        detail = engine.run_detail(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"unknown run '{run_id}'")
        return detail

    @v1.delete("/runs/{run_id}", status_code=202)
    async def cancel_run(run_id: str) -> dict[str, Any]:
        if not engine.request_abort(run_id):
            raise HTTPException(
                status_code=409, detail="run is not running (or unknown)"
            )
        return {"aborting": run_id}

    # ── collections / config / orphans ───────────────────────────────────────

    @v1.get("/collections")
    async def list_collections() -> list[dict[str, Any]]:
        return engine.collections()

    @v1.get("/config")
    async def get_config() -> dict[str, Any]:
        return engine.config_info()

    @v1.post("/config/reload")
    async def reload_config() -> dict[str, Any]:
        engine.reload_config()
        return engine.config_info()

    @v1.get("/orphans")
    async def list_orphans() -> list[dict[str, Any]]:
        return engine.orphans()

    @v1.delete("/orphans/{job_id}")
    async def delete_orphan(job_id: str, confirm: bool = False) -> dict[str, int]:
        if not confirm:
            raise HTTPException(status_code=400, detail="pass ?confirm=true to delete")
        try:
            return engine.delete_orphan(job_id)
        except JobStillActiveError as exc:
            raise HTTPException(
                status_code=409, detail=f"job '{job_id}' is still in the catalog"
            ) from exc

    app.include_router(v1)
    return app
