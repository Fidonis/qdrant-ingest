"""Static bearer-token authentication for the REST surface.

The add-on network is not a sufficient boundary: a multi-tenant,
prompt-injectable, tool-executing chat runtime shares the bridge with this
port. Every /v1 route therefore requires the QI_API_TOKEN bearer; /health
stays free, /metrics is governed by QI_METRICS_AUTH.
"""

import hmac
import logging

from fastapi import HTTPException, Request

from config import Settings

log = logging.getLogger("api")


def check_bearer(request: Request, settings: Settings) -> None:
    """Constant-time token comparison; failures are logged without the token."""
    client = request.client.host if request.client else "unknown"
    if not settings.api_token:
        log.warning("rejected request from %s: QI_API_TOKEN is not configured", client)
        raise HTTPException(
            status_code=401,
            detail="api token not configured",
            headers={"WWW-Authenticate": "Bearer"},
        )
    header = request.headers.get("Authorization", "")
    scheme, _, candidate = header.partition(" ")
    if scheme.lower() != "bearer" or not candidate:
        log.warning("rejected request from %s: missing bearer token", client)
        raise HTTPException(
            status_code=401,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not hmac.compare_digest(candidate.encode(), settings.api_token.encode()):
        log.warning("rejected request from %s: invalid token", client)
        raise HTTPException(
            status_code=401,
            detail="invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
