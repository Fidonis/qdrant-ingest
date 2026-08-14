"""OIDC bearer-token validator backed by JWKS and OIDC discovery.

Mirrors the validator of the companion RBAC server: discovery and JWKS are
cached, an unknown ``kid`` triggers exactly one refresh before failing, and
the signing algorithm is derived from the JWK — never from the token header.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from jose import jwt
from jose.exceptions import ExpiredSignatureError, JWTError

logger = logging.getLogger("mcp.oidc")

# Asymmetric signing algorithms accepted from OIDC providers. Symmetric (HS*)
# and 'none' are excluded to prevent algorithm-confusion attacks: an attacker
# controlling only the token cannot mint an HS-signed token using the JWKS
# public key as the HMAC secret.
_ALLOWED_ALGS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"})


class InvalidTokenError(Exception):
    """The token could not be validated."""


@dataclass
class OIDCClaims:
    sub: str
    email: str | None = None
    preferred_username: str | None = None
    realm_roles: list[str] = field(default_factory=list)
    client_roles: list[str] = field(default_factory=list)

    @property
    def all_roles(self) -> set[str]:
        return set(self.realm_roles) | set(self.client_roles)


class OIDCValidator:
    """Validates OIDC access tokens against a remote JWKS endpoint."""

    def __init__(
        self,
        issuer_url: str,
        audience: str,
        jwks_cache_ttl: int = 3600,
        http_timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._issuer_url = issuer_url.rstrip("/")
        self._audience = audience
        self._jwks_cache_ttl = jwks_cache_ttl
        self._http_timeout = http_timeout
        self._transport = transport

        self._discovery: dict[str, Any] | None = None
        self._discovery_fetched_at: float = 0.0
        self._jwks: dict[str, Any] | None = None
        self._jwks_fetched_at: float = 0.0
        # Separate locks: a JWKS refresh must not serialize unrelated
        # discovery fetches, and vice versa.
        self._discovery_lock = asyncio.Lock()
        self._jwks_lock = asyncio.Lock()

    async def validate(self, token: str) -> OIDCClaims:
        """Validate signature, expiry, audience and issuer; return claims."""
        try:
            unverified_header = jwt.get_unverified_header(token)
        except JWTError as exc:
            logger.info("rejected token with malformed header")
            raise InvalidTokenError("malformed token header") from exc

        key = await self._resolve_key(unverified_header.get("kid"))
        alg = _algorithm_for_key(key)
        issuer = await self._issuer_for_validation()

        try:
            payload = jwt.decode(
                token,
                key,
                algorithms=[alg],
                audience=self._audience,
                issuer=issuer,
                options={
                    "verify_signature": True,
                    "verify_aud": True,
                    "verify_iss": True,
                    "verify_exp": True,
                },
            )
        except ExpiredSignatureError as exc:
            logger.info("rejected expired token")
            raise InvalidTokenError("token expired") from exc
        except JWTError as exc:
            # Never leak token contents into logs.
            logger.info("token validation failed: %s", exc.__class__.__name__)
            raise InvalidTokenError("token validation failed") from exc

        return _extract_claims(payload)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._http_timeout, transport=self._transport)

    async def _resolve_key(self, kid: str | None) -> dict[str, Any]:
        if kid is None:
            raise InvalidTokenError("token header missing 'kid'")
        jwks = await self._get_jwks()
        key = _find_key(jwks, kid)
        if key is None:
            # Possible key rotation; force one refresh before giving up.
            logger.info("unknown kid %s, refreshing JWKS", kid)
            jwks = await self._get_jwks(force_refresh=True)
            key = _find_key(jwks, kid)
        if key is None:
            raise InvalidTokenError("signing key not found in JWKS")
        return key

    async def _issuer_for_validation(self) -> str:
        discovery = await self._get_discovery()
        issuer = discovery.get("issuer", self._issuer_url)
        return str(issuer)

    async def _get_discovery(self) -> dict[str, Any]:
        if (
            self._discovery is not None
            and (time.monotonic() - self._discovery_fetched_at) < self._jwks_cache_ttl
        ):
            return self._discovery
        async with self._discovery_lock:
            if (
                self._discovery is not None
                and (time.monotonic() - self._discovery_fetched_at) < self._jwks_cache_ttl
            ):
                return self._discovery
            url = f"{self._issuer_url}/.well-known/openid-configuration"
            async with self._client() as client:
                response = await client.get(url)
                response.raise_for_status()
                self._discovery = dict(response.json())
                self._discovery_fetched_at = time.monotonic()
        return self._discovery

    async def _get_jwks(self, *, force_refresh: bool = False) -> dict[str, Any]:
        if (
            not force_refresh
            and self._jwks is not None
            and (time.monotonic() - self._jwks_fetched_at) < self._jwks_cache_ttl
        ):
            return self._jwks
        discovery = await self._get_discovery()
        async with self._jwks_lock:
            if (
                not force_refresh
                and self._jwks is not None
                and (time.monotonic() - self._jwks_fetched_at) < self._jwks_cache_ttl
            ):
                return self._jwks
            jwks_uri = discovery.get("jwks_uri")
            if not jwks_uri:
                raise InvalidTokenError("OIDC discovery missing 'jwks_uri'")
            async with self._client() as client:
                response = await client.get(jwks_uri)
                response.raise_for_status()
                self._jwks = dict(response.json())
                self._jwks_fetched_at = time.monotonic()
        return self._jwks


def _find_key(jwks: dict[str, Any], kid: str) -> dict[str, Any] | None:
    for key in jwks.get("keys", []):
        if key.get("kid") != kid:
            continue
        # `use` is optional in RFC 7517; when present it must be "sig".
        if key.get("use") not in (None, "sig"):
            continue
        return dict(key)
    return None


def _algorithm_for_key(key: dict[str, Any]) -> str:
    """The signing algorithm to verify with, derived from the JWK."""
    alg = key.get("alg")
    if alg is None:
        # `alg` is RECOMMENDED but not REQUIRED in RFC 7517. Fall back per key
        # type rather than trusting the token header.
        kty = key.get("kty")
        if kty == "RSA":
            return "RS256"
        if kty == "EC":
            return "ES256"
        raise InvalidTokenError(f"unsupported JWK key type: {kty!r}")
    if alg not in _ALLOWED_ALGS:
        raise InvalidTokenError(f"JWK algorithm not permitted: {alg!r}")
    return str(alg)


def _extract_claims(payload: dict[str, Any]) -> OIDCClaims:
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        raise InvalidTokenError("token missing required 'sub' claim")
    try:
        realm_access = payload.get("realm_access") or {}
        resource_access = payload.get("resource_access") or {}
        realm_roles = list(realm_access.get("roles") or [])
        client_roles: list[str] = []
        for entry in resource_access.values():
            client_roles.extend((entry or {}).get("roles") or [])
    except (AttributeError, TypeError) as exc:
        raise InvalidTokenError("token has malformed role claims") from exc
    return OIDCClaims(
        sub=sub,
        email=payload.get("email"),
        preferred_username=payload.get("preferred_username"),
        realm_roles=realm_roles,
        client_roles=client_roles,
    )
