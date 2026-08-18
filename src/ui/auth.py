"""OIDC Authorization Code flow with PKCE for the operator web interface.

The MCP endpoint is a bearer resource server: a token arrives, it is validated,
that is the whole story. A browser cannot do that -- it has to be sent to the
provider and back -- so this module adds the one flow the service was missing,
and nothing else.

The cryptography is not repeated here. :class:`mcp_app.oidc.OIDCValidator`
already caches discovery and JWKS, refreshes once on an unknown ``kid`` and
derives the signing algorithm from the key rather than the token header; this
module drives it with the interface's own client id as the audience.
"""

import base64
import hashlib
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from config import Settings
from mcp_app.oidc import InvalidTokenError, OIDCValidator

log = logging.getLogger("ui.auth")

_HTTP_TIMEOUT = 10.0


class LoginError(Exception):
    """The login could not be completed; the reason is for the log, not the page."""


@dataclass(frozen=True)
class SessionUser:
    """The authenticated account, as stored in the signed session cookie."""

    sub: str
    username: str
    roles: tuple[str, ...]
    expires_at: int

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def to_dict(self) -> dict[str, Any]:
        return {
            "sub": self.sub,
            "username": self.username,
            "roles": list(self.roles),
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "SessionUser | None":
        """Rebuild from the session, returning None for anything unexpected.

        A session cookie that does not parse is treated as no session at all.
        It is signed, so this is a format change across a version rather than
        tampering -- and sending the operator to the login page beats a 500.
        """
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                sub=str(data["sub"]),
                username=str(data.get("username") or data["sub"]),
                roles=tuple(str(role) for role in data.get("roles") or ()),
                expires_at=int(data["expires_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


def pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    raw = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=")
    verifier = raw.decode()
    digest = hashlib.sha256(raw).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def new_state() -> str:
    return secrets.token_urlsafe(32)


class LoginFlow:
    """Drives the browser half of OIDC for one configured client."""

    def __init__(self, settings: Settings, validator: OIDCValidator) -> None:
        self._settings = settings
        self._validator = validator

    async def _discovery(self) -> dict[str, Any]:
        """Discovery, with transport failures folded into LoginError.

        A wrong issuer URL or an identity provider that is down are the two
        most likely first-run mistakes, and both surface here. They deserve
        "the provider could not be reached", not a stack trace.
        """
        try:
            return await self._validator.discovery()
        except httpx.HTTPError as exc:
            raise LoginError(f"OIDC discovery failed: {exc.__class__.__name__}") from exc

    async def authorization_url(self, state: str, challenge: str) -> str:
        endpoints = await self._discovery()
        authorize = endpoints.get("authorization_endpoint")
        if not authorize:
            raise LoginError("discovery document has no authorization_endpoint")
        params = {
            "response_type": "code",
            "client_id": self._settings.ui_client_id,
            "redirect_uri": self._settings.ui_redirect_uri,
            "scope": "openid profile",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{authorize}?{urlencode(params)}"

    async def logout_url(self, post_logout_redirect: str) -> str | None:
        endpoints = await self._discovery()
        end_session = endpoints.get("end_session_endpoint")
        if not end_session:
            return None
        params = {
            "client_id": self._settings.ui_client_id,
            "post_logout_redirect_uri": post_logout_redirect,
        }
        return f"{end_session}?{urlencode(params)}"

    async def complete(self, *, code: str, code_verifier: str) -> SessionUser:
        """Exchange the code and return the account behind it."""
        tokens = await self._exchange(code=code, code_verifier=code_verifier)

        id_token = tokens.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise LoginError("token response carried no id_token")

        try:
            claims = await self._validator.validate(id_token)
        except InvalidTokenError as exc:
            raise LoginError(f"id_token rejected: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LoginError(f"JWKS unreachable: {exc.__class__.__name__}") from exc

        roles = set(claims.realm_roles) | set(claims.client_roles)

        # Keycloak puts realm roles in the access token, not the ID token,
        # unless someone added a mapper. Read them from there when the ID
        # token carries none -- still fully verified, only the audience check
        # is skipped, because an access token is addressed to `account`.
        access_token = tokens.get("access_token")
        if not roles and isinstance(access_token, str) and access_token:
            try:
                access_claims = await self._validator.validate(access_token, verify_aud=False)
            except InvalidTokenError as exc:
                raise LoginError(f"access_token rejected: {exc}") from exc
            except httpx.HTTPError as exc:
                raise LoginError(f"JWKS unreachable: {exc.__class__.__name__}") from exc
            roles = set(access_claims.realm_roles) | set(access_claims.client_roles)

        lifetime = self._settings.ui_session_ttl
        return SessionUser(
            sub=claims.sub,
            username=claims.preferred_username or claims.email or claims.sub,
            roles=tuple(sorted(roles)),
            expires_at=int(time.time()) + lifetime,
        )

    async def _exchange(self, *, code: str, code_verifier: str) -> dict[str, Any]:
        endpoints = await self._discovery()
        token_endpoint = endpoints.get("token_endpoint")
        if not token_endpoint:
            raise LoginError("discovery document has no token_endpoint")

        form = {
            "grant_type": "authorization_code",
            "client_id": self._settings.ui_client_id,
            "client_secret": self._settings.ui_client_secret,
            "redirect_uri": self._settings.ui_redirect_uri,
            "code": code,
            "code_verifier": code_verifier,
        }
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                response = await client.post(token_endpoint, data=form)
        except httpx.HTTPError as exc:
            raise LoginError(f"token endpoint unreachable: {exc.__class__.__name__}") from exc

        if response.status_code != 200:
            # The body can carry the client secret back in an error echo, so
            # only the status reaches the log.
            log.warning("token endpoint returned HTTP %d", response.status_code)
            raise LoginError("token exchange failed")

        payload: dict[str, Any] = response.json()
        return payload
