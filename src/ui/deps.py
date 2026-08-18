"""Session, CSRF and role gates for the web interface.

The three rules enforced here are the whole authorization story of the
interface, so they live in one module rather than being repeated per route:

* a request without a valid, unexpired session never reaches a handler;
* a request without the operator realm role never reaches a handler;
* a mutating request without a matching CSRF token never reaches a handler.

None of it touches the REST or MCP planes. A session is a way into ``/ui`` and
nothing else -- ``/v1`` keeps demanding its bearer token and ``/mcp`` keeps
demanding a Keycloak token, whoever is logged in here.
"""

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from ui.auth import SessionUser

SESSION_USER_KEY = "user"
SESSION_CSRF_KEY = "csrf"
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf_token"


class NotAuthenticatedError(Exception):
    """No usable session. Handled by a redirect to the login route."""


def current_user(request: Request) -> SessionUser:
    """Return the signed-in account, or send the browser to the login route."""
    user = SessionUser.from_dict(request.session.get(SESSION_USER_KEY))
    if user is None:
        raise NotAuthenticatedError
    if user.is_expired:
        request.session.clear()
        raise NotAuthenticatedError
    return user


def require_operator(request: Request) -> SessionUser:
    """Reject an account that lacks the operator realm role.

    The same role the MCP endpoint requires. Reusing it is deliberate: an
    operator who may drive ingestion through a language model may drive it
    through a browser, and a second role would be a second manual step in
    Keycloak for no additional protection.
    """
    user = current_user(request)
    required = request.app.state.settings.oidc_operator_role
    if not user.has_role(required):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"realm role {required!r} required",
        )
    return user


def csrf_token(request: Request) -> str:
    """Return the session CSRF token, minting one on first use."""
    token = request.session.get(SESSION_CSRF_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_CSRF_KEY] = token
    return token


async def verify_csrf(request: Request) -> None:
    """Reject a mutating request whose CSRF token is absent or wrong.

    Accepts the token from the header htmx sends on every request, or from a
    form field for the plain-form fallback. Compared in constant time.
    """
    expected = request.session.get(SESSION_CSRF_KEY)
    if not isinstance(expected, str) or not expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="no csrf token in session"
        )

    submitted = request.headers.get(CSRF_HEADER)
    if submitted is None:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")
        ):
            form = await request.form()
            value = form.get(CSRF_FIELD)
            submitted = value if isinstance(value, str) else None

    if not submitted or not secrets.compare_digest(submitted, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="csrf token invalid")


Operator = Annotated[SessionUser, Depends(require_operator)]
CsrfChecked = Annotated[None, Depends(verify_csrf)]
