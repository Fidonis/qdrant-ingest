"""Routes of the operator web interface.

Mutations are plain form posts answered with a 303 redirect, not fetch calls:
the whole interface then works without JavaScript having booted, and the
back button does the obvious thing. htmx is used for the two things a
redirect cannot do -- polling the health strip and the run list.
"""

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Form, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse, Response

from catalog.writer import (
    CatalogWriteError,
    dump_document,
    find_job,
    load_document,
    migrate_legacy,
    read_raw,
    remove_job,
    resolve_location,
    upsert_job,
    write_raw,
)
from engine.locks import RunRejectedError
from engine.runner import Mode
from engine.service import JobEngine, JobStillActiveError, UnknownJobError
from ui import forms
from ui.auth import LoginError, LoginFlow, new_state, pkce_pair
from ui.deps import (
    CSRF_FIELD,
    Operator,
    csrf_token,
    require_operator,
    verify_csrf,
)
from ui.templating import templates

log = logging.getLogger("ui.routes")

STATIC_DIR = Path(__file__).resolve().parent / "static"

router = APIRouter()

_STATE_KEY = "oauth_state"
_VERIFIER_KEY = "oauth_verifier"
_FLASH_KEY = "flash"


# -- shared context ---------------------------------------------------------


def _engine(request: Request) -> JobEngine:
    engine: JobEngine = request.app.state.engine
    return engine


def _environ(request: Request) -> Mapping[str, str]:
    environ: Mapping[str, str] | None = request.app.state.environ
    return os.environ if environ is None else environ


def _flash(request: Request, level: str, message: str) -> None:
    """Queue a one-shot message to be shown after the next redirect."""
    pending = request.session.get(_FLASH_KEY) or []
    pending.append({"level": level, "message": message})
    request.session[_FLASH_KEY] = pending


def _take_flashes(request: Request) -> list[dict[str, str]]:
    pending = request.session.pop(_FLASH_KEY, None) or []
    return [entry for entry in pending if isinstance(entry, dict)]


def _ctx(request: Request, user: Any = None, **extra: Any) -> dict[str, Any]:
    settings = request.app.state.settings
    location = resolve_location(settings)
    context: dict[str, Any] = {
        "settings": settings,
        "user": user,
        "csrf_token": csrf_token(request),
        "csrf_field": CSRF_FIELD,
        "ui_path": settings.ui_path.rstrip("/"),
        "catalog": location,
        "flashes": _take_flashes(request),
        "active": "",
    }
    context.update(extra)
    return context


def _redirect(request: Request, path: str) -> RedirectResponse:
    base = request.app.state.settings.ui_path.rstrip("/")
    return RedirectResponse(f"{base}{path}", status_code=status.HTTP_303_SEE_OTHER)


def _job_or_404(engine: JobEngine, job_id: str) -> Any:
    try:
        return engine.get_job(job_id)
    except UnknownJobError as exc:
        raise HTTPException(status_code=404, detail=f"unknown job {job_id!r}") from exc


# -- authentication ---------------------------------------------------------


@router.get("/auth/login")
async def login(request: Request) -> Response:
    settings = request.app.state.settings
    flow = LoginFlow(settings, request.app.state.validator)
    state = new_state()
    verifier, challenge = pkce_pair()
    request.session[_STATE_KEY] = state
    request.session[_VERIFIER_KEY] = verifier
    try:
        url = await flow.authorization_url(state, challenge)
    except LoginError as exc:
        log.warning("login could not start: %s", exc)
        raise HTTPException(status_code=502, detail="identity provider unreachable") from exc
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/auth/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    if error:
        # The provider's own wording is not shown; it is attacker-influencable
        # in some flows and adds nothing an operator can act on.
        log.info("identity provider returned an error response")
        raise HTTPException(status_code=401, detail="login was not completed")

    expected_state = request.session.pop(_STATE_KEY, None)
    verifier = request.session.pop(_VERIFIER_KEY, None)
    if not code or not state or not expected_state or state != expected_state or not verifier:
        raise HTTPException(status_code=400, detail="login state did not match; start again")

    settings = request.app.state.settings
    flow = LoginFlow(settings, request.app.state.validator)
    try:
        user = await flow.complete(code=code, code_verifier=verifier)
    except LoginError as exc:
        log.warning("login failed: %s", exc)
        raise HTTPException(status_code=401, detail="login failed") from exc

    if not user.has_role(settings.oidc_operator_role):
        # Refused before a session exists: an account without the role should
        # not end up holding a cookie for a surface it may not use.
        log.info("rejected login for an account without the operator role")
        raise HTTPException(
            status_code=403,
            detail=f"realm role {settings.oidc_operator_role!r} required",
        )

    request.session["user"] = user.to_dict()
    return _redirect(request, "/")


@router.post("/auth/logout")
async def logout(request: Request, csrf_token: str = Form(alias=CSRF_FIELD)) -> Response:
    await verify_csrf(request)
    settings = request.app.state.settings
    request.session.clear()
    flow = LoginFlow(settings, request.app.state.validator)
    try:
        url = await flow.logout_url(f"{settings.ui_public_url.rstrip('/')}{settings.ui_path}")
    except LoginError:
        url = None
    if url:
        return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
    return _redirect(request, "/auth/login")


# -- dashboard --------------------------------------------------------------


@router.get("/")
def dashboard(request: Request, user: Operator) -> Response:
    engine = _engine(request)
    runs = engine.list_runs(limit=8)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _ctx(
            request,
            user,
            active="dashboard",
            health=engine.health(),
            config=engine.config_info(),
            jobs=[engine.job_summary(job) for job in engine.jobs()],
            runs=[run.as_dict() for run in runs],
        ),
    )


@router.get("/partials/health")
def partial_health(request: Request, user: Operator) -> Response:
    engine = _engine(request)
    return templates.TemplateResponse(
        request,
        "partials/health.html",
        _ctx(request, user, health=engine.health(), config=engine.config_info()),
    )


# -- jobs -------------------------------------------------------------------


@router.get("/jobs")
def jobs_page(request: Request, user: Operator) -> Response:
    engine = _engine(request)
    return templates.TemplateResponse(
        request,
        "jobs.html",
        _ctx(
            request,
            user,
            active="jobs",
            jobs=[engine.job_summary(job) for job in engine.jobs()],
            config=engine.config_info(),
        ),
    )


@router.get("/jobs/new")
def job_new(request: Request, user: Operator) -> Response:
    return templates.TemplateResponse(
        request,
        "job_edit.html",
        _ctx(
            request,
            user,
            active="jobs",
            values=forms.blank_form_values(),
            original_id="",
            source_fields=forms.SOURCE_FIELDS,
            source_types=forms.SOURCE_TYPES,
            modes=forms.MODES,
            chunk_strategies=forms.CHUNK_STRATEGIES,
            startup_policies=forms.STARTUP_POLICIES,
            secret_names=forms.available_secret_names(_environ(request)),
            errors=[],
        ),
    )


@router.get("/jobs/{job_id}")
def job_detail(request: Request, user: Operator, job_id: str) -> Response:
    engine = _engine(request)
    job = _job_or_404(engine, job_id)
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        _ctx(
            request,
            user,
            active="jobs",
            job=engine.job_detail(job),
            summary=engine.job_summary(job),
            job_id=job_id,
        ),
    )


@router.get("/jobs/{job_id}/edit")
def job_edit(request: Request, user: Operator, job_id: str) -> Response:
    settings = request.app.state.settings
    location = resolve_location(settings)
    document = load_document(read_raw(location))
    raw_job = find_job(document, job_id)
    if raw_job is None:
        raise HTTPException(status_code=404, detail=f"unknown job {job_id!r}")
    return templates.TemplateResponse(
        request,
        "job_edit.html",
        _ctx(
            request,
            user,
            active="jobs",
            values=forms.form_values_from_job(raw_job),
            original_id=job_id,
            source_fields=forms.SOURCE_FIELDS,
            source_types=forms.SOURCE_TYPES,
            modes=forms.MODES,
            chunk_strategies=forms.CHUNK_STRATEGIES,
            startup_policies=forms.STARTUP_POLICIES,
            secret_names=forms.available_secret_names(_environ(request)),
            errors=[],
        ),
    )


@router.post("/jobs/save")
async def job_save(request: Request) -> Response:
    user = require_operator(request)
    await verify_csrf(request)
    settings = request.app.state.settings
    form = await request.form()
    original_id = str(form.get("original_id") or "")

    def _redisplay(messages: list[str]) -> Response:
        return templates.TemplateResponse(
            request,
            "job_edit.html",
            _ctx(
                request,
                user,
                active="jobs",
                values=dict(form),
                original_id=original_id,
                source_fields=forms.SOURCE_FIELDS,
                source_types=forms.SOURCE_TYPES,
                modes=forms.MODES,
                chunk_strategies=forms.CHUNK_STRATEGIES,
                startup_policies=forms.STARTUP_POLICIES,
                secret_names=forms.available_secret_names(_environ(request)),
                errors=messages,
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    try:
        job = forms.job_from_form(form)
    except forms.FormError as exc:
        return _redisplay([str(exc)])

    location = resolve_location(settings)
    try:
        document = load_document(read_raw(location))
        upsert_job(document, job, original_id or None)
        write_raw(location, dump_document(document), settings, _environ(request))
    except CatalogWriteError as exc:
        return _redisplay([f"{issue.field}: {issue.message}" for issue in exc.issues])

    _engine(request).reload_config()
    _flash(request, "success", f"Job {job['id']} saved.")
    return _redirect(request, f"/jobs/{job['id']}")


@router.post("/jobs/{job_id}/delete")
async def job_delete(request: Request, job_id: str) -> Response:
    require_operator(request)
    await verify_csrf(request)
    settings = request.app.state.settings
    location = resolve_location(settings)
    try:
        document = load_document(read_raw(location))
        remove_job(document, job_id)
        write_raw(location, dump_document(document), settings, _environ(request))
    except CatalogWriteError as exc:
        _flash(request, "error", str(exc))
        return _redirect(request, f"/jobs/{job_id}")

    _engine(request).reload_config()
    _flash(
        request,
        "warning",
        f"Job {job_id} removed. Points already written stay in Qdrant and now "
        f"show up under orphans.",
    )
    return _redirect(request, "/jobs")


@router.post("/jobs/{job_id}/run")
async def job_run(request: Request, job_id: str) -> Response:
    require_operator(request)
    await verify_csrf(request)
    engine = _engine(request)
    _job_or_404(engine, job_id)
    form = await request.form()
    dry_run = str(form.get("dry_run") or "").lower() in ("1", "true", "on")

    # An empty choice means "as configured". Anything else has to be one of the
    # three modes -- the select offers no others, but the request is the
    # operator's to shape, and a mode the engine does not know is not a
    # harmless typo when the candidates include a destructive rebuild.
    requested = str(form.get("mode") or "")
    mode: Mode | None = None
    if requested:
        if requested not in forms.MODES:
            raise HTTPException(status_code=400, detail=f"unknown mode {requested!r}")
        mode = cast(Mode, requested)

    try:
        result = engine.trigger_run(
            job_id, "manual_ui", mode=mode, dry_run=dry_run, queue=False
        )
    except RunRejectedError as exc:
        _flash(request, "warning", f"Job {job_id} is already running (run {exc.active_run_id}).")
        return _redirect(request, f"/jobs/{job_id}")

    label = "Dry run" if dry_run else "Run"
    _flash(request, "success", f"{label} started for {job_id}.")
    return _redirect(request, f"/runs/{result['run_id']}")


@router.post("/jobs/{job_id}/pause")
async def job_pause(request: Request, job_id: str) -> Response:
    require_operator(request)
    await verify_csrf(request)
    engine = _engine(request)
    _job_or_404(engine, job_id)
    engine.pause_job(job_id)
    _flash(request, "success", f"Job {job_id} paused; scheduled runs will not fire.")
    return _redirect(request, f"/jobs/{job_id}")


@router.post("/jobs/{job_id}/resume")
async def job_resume(request: Request, job_id: str) -> Response:
    require_operator(request)
    await verify_csrf(request)
    engine = _engine(request)
    _job_or_404(engine, job_id)
    engine.resume_job(job_id)
    _flash(request, "success", f"Job {job_id} resumed.")
    return _redirect(request, f"/jobs/{job_id}")


@router.get("/jobs/{job_id}/preview")
def job_preview(
    request: Request,
    user: Operator,
    job_id: str,
    limit: int = Query(default=50, ge=1, le=500),
) -> Response:
    engine = _engine(request)
    _job_or_404(engine, job_id)
    return templates.TemplateResponse(
        request,
        "partials/preview.html",
        _ctx(request, user, entries=engine.preview(job_id, limit), job_id=job_id),
    )


# -- catalog ----------------------------------------------------------------


@router.get("/catalog")
def catalog_page(request: Request, user: Operator) -> Response:
    settings = request.app.state.settings
    location = resolve_location(settings)
    return templates.TemplateResponse(
        request,
        "catalog.html",
        _ctx(
            request,
            user,
            active="catalog",
            raw=read_raw(location),
            config=_engine(request).config_info(),
        ),
    )


@router.post("/catalog/save")
async def catalog_save(request: Request) -> Response:
    user = require_operator(request)
    await verify_csrf(request)
    settings = request.app.state.settings
    form = await request.form()
    raw = str(form.get("raw") or "")
    location = resolve_location(settings)
    try:
        write_raw(location, raw, settings, _environ(request))
    except CatalogWriteError as exc:
        return templates.TemplateResponse(
            request,
            "catalog.html",
            _ctx(
                request,
                user,
                active="catalog",
                raw=raw,
                config=_engine(request).config_info(),
                errors=[f"{issue.field}: {issue.message}" for issue in exc.issues],
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    _engine(request).reload_config()
    _flash(request, "success", "Catalog saved and reloaded.")
    return _redirect(request, "/catalog")


@router.post("/catalog/migrate")
async def catalog_migrate(request: Request) -> Response:
    require_operator(request)
    await verify_csrf(request)
    settings = request.app.state.settings
    try:
        migrate_legacy(settings, _environ(request))
    except CatalogWriteError as exc:
        _flash(request, "error", str(exc))
        return _redirect(request, "/catalog")

    _engine(request).reload_config()
    _flash(
        request,
        "success",
        f"Catalog copied to {settings.jobs_file}. The old file is still there and "
        f"is no longer read.",
    )
    return _redirect(request, "/catalog")


@router.post("/catalog/reload")
async def catalog_reload(request: Request) -> Response:
    require_operator(request)
    await verify_csrf(request)
    result = _engine(request).reload_config()
    if result.ok:
        _flash(request, "success", f"Catalog reloaded: {len(result.jobs)} job(s).")
    else:
        _flash(request, "error", result.config_error or "catalog did not load")
    return _redirect(request, "/catalog")


# -- runs -------------------------------------------------------------------


@router.get("/runs")
def runs_page(
    request: Request,
    user: Operator,
    job_id: str | None = None,
    run_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
) -> Response:
    engine = _engine(request)
    runs = engine.list_runs(job_id=job_id, status=run_status, limit=limit)
    return templates.TemplateResponse(
        request,
        "runs.html",
        _ctx(
            request,
            user,
            active="runs",
            runs=[run.as_dict() for run in runs],
            job_ids=[job.id for job in engine.jobs()],
            filter_job_id=job_id or "",
            filter_status=run_status or "",
        ),
    )


@router.get("/partials/runs")
def partial_runs(
    request: Request,
    user: Operator,
    job_id: str | None = None,
    run_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
) -> Response:
    engine = _engine(request)
    runs = engine.list_runs(job_id=job_id, status=run_status, limit=limit)
    return templates.TemplateResponse(
        request,
        "partials/run_rows.html",
        _ctx(request, user, runs=[run.as_dict() for run in runs]),
    )


@router.get("/runs/{run_id}")
def run_detail(request: Request, user: Operator, run_id: str) -> Response:
    detail = _engine(request).run_detail(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        _ctx(request, user, active="runs", detail=detail, run_id=run_id),
    )


@router.post("/runs/{run_id}/abort")
async def run_abort(request: Request, run_id: str) -> Response:
    require_operator(request)
    await verify_csrf(request)
    if not _engine(request).request_abort(run_id):
        _flash(request, "warning", "That run is not running any more.")
    else:
        _flash(request, "success", "Abort requested; the run stops after the current document.")
    return _redirect(request, f"/runs/{run_id}")


# -- collections and orphans ------------------------------------------------


@router.get("/collections")
def collections_page(request: Request, user: Operator) -> Response:
    return templates.TemplateResponse(
        request,
        "collections.html",
        _ctx(request, user, active="collections", collections=_engine(request).collections()),
    )


@router.get("/orphans")
def orphans_page(request: Request, user: Operator) -> Response:
    return templates.TemplateResponse(
        request,
        "orphans.html",
        _ctx(request, user, active="orphans", orphans=_engine(request).orphans()),
    )


@router.post("/orphans/{job_id}/delete")
async def orphan_delete(request: Request, job_id: str) -> Response:
    require_operator(request)
    await verify_csrf(request)
    try:
        result = _engine(request).delete_orphan(job_id)
    except JobStillActiveError:
        _flash(request, "error", f"Job {job_id} is still in the catalog; remove it there first.")
        return _redirect(request, "/orphans")

    _flash(
        request,
        "success",
        f"Removed {result['deleted_points']} point(s) and {result['deleted_rows']} "
        f"state row(s) for {job_id}.",
    )
    return _redirect(request, "/orphans")
