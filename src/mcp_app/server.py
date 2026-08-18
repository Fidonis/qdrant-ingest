"""The MCP server and its OIDC gate.

A raw ASGI middleware, deliberately not Starlette's ``BaseHTTPMiddleware``:
the streamable-HTTP transport streams its responses, and BaseHTTPMiddleware
would buffer them.

The add-on network is not a sufficient boundary — a multi-tenant,
prompt-injectable, tool-executing chat runtime sits on the same bridge — so
every MCP request must present a bearer token that validates against the
configured issuer, carries the expected audience, and grants the operator
realm role.
"""

import json
import logging
from typing import Any

from fastmcp import FastMCP
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from config import APP_NAME
from engine.service import JobEngine
from mcp_app.oidc import InvalidTokenError, OIDCClaims, OIDCValidator
from mcp_app.tools import register_tools

logger = logging.getLogger("mcp.server")

STATE_CLAIMS = "oidc_claims"


class OIDCAuthMiddleware:
    """Validates the bearer token and enforces the operator realm role."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        validator: OIDCValidator,
        operator_role: str,
    ) -> None:
        self.app = app
        self._validator = validator
        self._operator_role = operator_role

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        header = _get_header(scope, b"authorization")
        if not header or not header.lower().startswith("bearer "):
            await _respond_json(send, 401, {"error": "missing_bearer_token"})
            return

        token = header[7:].strip()
        try:
            claims = await self._validator.validate(token)
        except InvalidTokenError as exc:
            logger.info("MCP.deny invalid token: %s", exc)
            await _respond_json(send, 401, {"error": "invalid_token"})
            return
        except Exception:
            logger.exception("unexpected error during OIDC validation")
            await _respond_json(send, 500, {"error": "auth_internal_error"})
            return

        if self._operator_role and self._operator_role not in claims.all_roles:
            logger.warning(
                "MCP.deny sub=%s lacks the operator role '%s'",
                claims.sub,
                self._operator_role,
            )
            await _respond_json(send, 403, {"error": "missing_operator_role"})
            return

        state = scope.setdefault("state", {})
        state[STATE_CLAIMS] = claims
        await self.app(scope, receive, send)


def build_mcp_server(engine: JobEngine) -> FastMCP:
    mcp: FastMCP = FastMCP(APP_NAME)
    register_tools(mcp, engine)
    return mcp


def build_mcp_app(
    engine: JobEngine,
    validator: OIDCValidator,
    operator_role: str,
    path: str = "/",
) -> ASGIApp:
    """The MCP ASGI app, wrapped in its OIDC gate.

    ``path`` is the path the transport answers on, and it must be the same path
    the parent application routes to it: the app is registered as an exact
    route, so the request scope arrives unchanged rather than stripped of a
    mount prefix.
    """
    mcp = build_mcp_server(engine)
    app = mcp.http_app(path=path)
    guarded = OIDCAuthMiddleware(app, validator=validator, operator_role=operator_role)
    # The sub-app keeps its own lifespan; the caller passes it to the parent
    # application so the MCP session manager starts and stops with it.
    guarded.lifespan = app.lifespan  # type: ignore[attr-defined]
    return guarded


def _get_header(scope: Scope, name: bytes) -> str | None:
    headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
    for raw_name, raw_value in headers:
        if raw_name.lower() == name:
            return raw_value.decode("latin-1")
    return None


async def _respond_json(send: Send, status: int, body: dict[str, Any]) -> None:
    payload = json.dumps(body).encode("utf-8")
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode("ascii")),
    ]
    if status == 401:
        headers.append(
            (b"www-authenticate", b'Bearer realm="mcp", error="invalid_token"')
        )
    start: Message = {"type": "http.response.start", "status": status, "headers": headers}
    await send(start)
    await send({"type": "http.response.body", "body": payload})


__all__ = ["OIDCAuthMiddleware", "OIDCClaims", "build_mcp_app", "build_mcp_server"]
