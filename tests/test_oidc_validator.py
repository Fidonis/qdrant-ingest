"""JWKS-backed token validation."""

import pytest

from mcp_app import InvalidTokenError, OIDCValidator

from fakes.oidc import AUDIENCE, ISSUER, OPERATOR_ROLE, FakeIssuer


@pytest.fixture
def issuer() -> FakeIssuer:
    return FakeIssuer()


def _validator(issuer: FakeIssuer, audience: str = AUDIENCE) -> OIDCValidator:
    return OIDCValidator(ISSUER, audience, transport=issuer.transport())


async def test_valid_token_yields_claims(issuer: FakeIssuer) -> None:
    claims = await _validator(issuer).validate(issuer.token(sub="alice"))
    assert claims.sub == "alice"
    assert OPERATOR_ROLE in claims.all_roles


async def test_client_roles_are_collected(issuer: FakeIssuer) -> None:
    import time

    from jose import jwt

    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "bob",
            "aud": AUDIENCE,
            "iss": ISSUER,
            "iat": now,
            "exp": now + 600,
            "resource_access": {"librechat": {"roles": ["some-client-role"]}},
        },
        issuer.private_pem,
        algorithm="RS256",
        headers={"kid": issuer.kid},
    )
    claims = await _validator(issuer).validate(token)
    assert claims.all_roles == {"some-client-role"}


async def test_expired_token_rejected(issuer: FakeIssuer) -> None:
    with pytest.raises(InvalidTokenError, match="expired"):
        await _validator(issuer).validate(issuer.token(expires_in=-60))


async def test_wrong_audience_rejected(issuer: FakeIssuer) -> None:
    with pytest.raises(InvalidTokenError):
        await _validator(issuer).validate(issuer.token(audience="some-other-client"))


async def test_wrong_issuer_rejected(issuer: FakeIssuer) -> None:
    with pytest.raises(InvalidTokenError):
        await _validator(issuer).validate(issuer.token(issuer="https://evil.test"))


async def test_malformed_token_rejected(issuer: FakeIssuer) -> None:
    with pytest.raises(InvalidTokenError, match="malformed"):
        await _validator(issuer).validate("not-a-jwt")


async def test_unknown_kid_rejected_after_one_refresh(issuer: FakeIssuer) -> None:
    validator = _validator(issuer)
    await validator.validate(issuer.token())  # warm the caches
    calls_before = issuer.jwks_calls
    with pytest.raises(InvalidTokenError, match="signing key"):
        await validator.validate(issuer.token(kid="rotated-away"))
    # Exactly one forced refresh, not an unbounded retry loop.
    assert issuer.jwks_calls == calls_before + 1


async def test_rotated_key_is_picked_up_by_the_refresh(issuer: FakeIssuer) -> None:
    validator = _validator(issuer)
    await validator.validate(issuer.token())
    # The provider rotates: the same material is now served under a new kid.
    issuer.extra_jwks = [issuer.public_jwk(kid="key-2")]
    claims = await validator.validate(issuer.token(kid="key-2"))
    assert claims.sub == "user-1"


async def test_hs256_confusion_is_refused(issuer: FakeIssuer) -> None:
    # An attacker signs with HMAC using the public key material as the secret;
    # deriving the algorithm from the JWK (RS256) makes this fail.
    forged = issuer.token(algorithm="HS256", key="public-key-as-hmac-secret")
    with pytest.raises(InvalidTokenError):
        await _validator(issuer).validate(forged)


async def test_caches_avoid_refetching(issuer: FakeIssuer) -> None:
    validator = _validator(issuer)
    for _ in range(3):
        await validator.validate(issuer.token())
    assert issuer.discovery_calls == 1
    assert issuer.jwks_calls == 1
