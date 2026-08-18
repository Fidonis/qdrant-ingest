"""The operator web interface, as a sub-application mounted under ui_path.

A mounted application rather than a router, for three reasons that all point
the same way: the session middleware then covers the interface and nothing
else, the static files mount without colliding with the API, and a request to
``/v1`` cannot pick up a session cookie on its way past.
"""

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from config import Settings
from engine.service import JobEngine
from mcp_app.oidc import OIDCValidator
from ui import routes
from ui.deps import NotAuthenticatedError
from ui.templating import set_asset_prefix, templates

log = logging.getLogger("ui")

SESSION_COOKIE = "qdrant_ingest_ui"


def build_ui_app(
    settings: Settings,
    engine: JobEngine,
    validator: OIDCValidator,
    environ: Mapping[str, str] | None = None,
) -> FastAPI:
    """Assemble the interface. The caller decides whether to mount it."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    app.state.settings = settings
    app.state.engine = engine
    app.state.validator = validator
    app.state.environ = environ

    mount_path = settings.ui_path.rstrip("/")
    set_asset_prefix(f"{mount_path}/static")

    # The cookie is restricted to the interface's own path: a request to /v1
    # or /mcp then does not carry it at all, which is a stronger statement
    # than "the API ignores sessions".
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.ui_session_secret,
        session_cookie=SESSION_COOKIE,
        max_age=settings.ui_session_ttl,
        same_site="lax",
        https_only=settings.ui_public_url.startswith("https://"),
        path=mount_path or "/",
    )

    app.include_router(routes.router)
    app.mount(
        "/static",
        StaticFiles(directory=str(routes.STATIC_DIR)),
        name="static",
    )

    @app.exception_handler(NotAuthenticatedError)
    async def _login_redirect(request: Request, _exc: NotAuthenticatedError) -> Response:
        target = f"{mount_path}/auth/login"
        # An htmx swap must not render the identity provider inside a panel;
        # this tells the client to navigate the whole page instead.
        if request.headers.get("hx-request"):
            return Response(status_code=204, headers={"HX-Redirect": target})
        return RedirectResponse(target, status_code=303)

    @app.exception_handler(HTTPException)
    async def _error_page(request: Request, exc: HTTPException) -> Response:
        if request.headers.get("hx-request"):
            return Response(str(exc.detail), status_code=exc.status_code)
        context: dict[str, Any] = {
            "status_code": exc.status_code,
            "detail": exc.detail,
            "settings": settings,
            "ui_path": mount_path,
        }
        return templates.TemplateResponse(
            request, "error.html", context, status_code=exc.status_code
        )

    return app


def attach_ui(
    parent: FastAPI,
    settings: Settings,
    engine: JobEngine,
    validator: OIDCValidator | None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Mount the interface onto the main application when it is configured."""
    if not settings.ui_active or validator is None:
        log.info(
            "web interface disabled; set QI_UI_PUBLIC_URL, QI_UI_CLIENT_SECRET, "
            "QI_UI_SESSION_SECRET and OIDC_ISSUER to enable it"
        )
        return
    parent.mount(settings.ui_path, build_ui_app(settings, engine, validator, environ))
    log.info("web interface mounted at %s", settings.ui_path)
