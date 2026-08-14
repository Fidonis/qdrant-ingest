"""A local OIDC issuer for tests: RSA keypair, JWKS, and token minting.

Everything runs in-process through httpx.MockTransport — no Keycloak, no
network, and the private key never leaves the test run.
"""

import time
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ISSUER = "https://idp.test/realms/papaia"
AUDIENCE = "mcp-qdrant-ingest"
OPERATOR_ROLE = "qdrant-ingest-operator"


class FakeIssuer:
    def __init__(self, kid: str = "test-key-1") -> None:
        self.kid = kid
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_pem = self._key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")
        self.discovery_calls = 0
        self.jwks_calls = 0
        # Extra keys served by the JWKS endpoint, e.g. after a rotation.
        self.extra_jwks: list[dict[str, Any]] = []
        # Serve the signing key without an `alg`, which is legal per RFC 7517
        # and exercises the validator's own algorithm fallback.
        self.omit_alg = False

    def public_jwk(self, kid: str | None = None, alg: str | None = "RS256") -> dict[str, Any]:
        jwk: dict[str, Any] = dict(RSAAlgorithm.to_jwk(self._key.public_key(), as_dict=True))
        jwk.update({"use": "sig", "kid": kid or self.kid})
        if alg is not None:
            jwk["alg"] = alg
        return jwk

    def jwks(self) -> dict[str, Any]:
        primary = self.public_jwk(alg=None if self.omit_alg else "RS256")
        return {"keys": [primary, *self.extra_jwks]}

    def token(
        self,
        *,
        sub: str = "user-1",
        audience: str = AUDIENCE,
        issuer: str = ISSUER,
        roles: list[str] | None = None,
        client_roles: dict[str, list[str]] | None = None,
        expires_in: int | None = 3600,
        kid: str | None = None,
        algorithm: str = "RS256",
        key: Any = None,
    ) -> str:
        """Mint a token. ``expires_in=None`` omits the `exp` claim entirely."""
        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": sub,
            "aud": audience,
            "iss": issuer,
            "iat": now,
            "realm_access": {"roles": roles if roles is not None else [OPERATOR_ROLE]},
        }
        if expires_in is not None:
            claims["exp"] = now + expires_in
        if client_roles is not None:
            claims["resource_access"] = {
                client: {"roles": granted} for client, granted in client_roles.items()
            }
        return jwt.encode(
            claims,
            key if key is not None else self.private_pem,
            algorithm=algorithm,
            headers={"kid": kid or self.kid},
        )

    def transport(self) -> httpx.MockTransport:
        async def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/.well-known/openid-configuration"):
                self.discovery_calls += 1
                return httpx.Response(
                    200,
                    json={
                        "issuer": ISSUER,
                        "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
                    },
                )
            if path.endswith("/certs"):
                self.jwks_calls += 1
                return httpx.Response(200, json=self.jwks())
            return httpx.Response(404, json={"error": "not found"})

        return httpx.MockTransport(handler)
