"""A local OIDC issuer for tests: RSA keypair, JWKS, and token minting.

Everything runs in-process through httpx.MockTransport — no Keycloak, no
network, and the private key never leaves the test run.
"""

import time
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt
from jose.utils import base64url_encode

ISSUER = "https://idp.test/realms/papaia"
AUDIENCE = "mcp-qdrant-ingest"
OPERATOR_ROLE = "qdrant-ingest-operator"


def _int_to_b64(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64url_encode(raw).decode("ascii")


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

    def public_jwk(self, kid: str | None = None) -> dict[str, Any]:
        numbers = self._key.public_key().public_numbers()
        return {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": kid or self.kid,
            "n": _int_to_b64(numbers.n),
            "e": _int_to_b64(numbers.e),
        }

    def jwks(self) -> dict[str, Any]:
        return {"keys": [self.public_jwk(), *self.extra_jwks]}

    def token(
        self,
        *,
        sub: str = "user-1",
        audience: str = AUDIENCE,
        issuer: str = ISSUER,
        roles: list[str] | None = None,
        expires_in: int = 3600,
        kid: str | None = None,
        algorithm: str = "RS256",
        key: Any = None,
    ) -> str:
        now = int(time.time())
        claims = {
            "sub": sub,
            "aud": audience,
            "iss": issuer,
            "iat": now,
            "exp": now + expires_in,
            "realm_access": {"roles": roles if roles is not None else [OPERATOR_ROLE]},
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
