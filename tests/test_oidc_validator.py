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
    token = issuer.token(
        sub="bob", roles=[], client_roles={"librechat": ["some-client-role"]}
    )
    claims = await _validator(issuer).validate(token)
    assert claims.all_roles == {"some-client-role"}


async def test_expired_token_rejected(issuer: FakeIssuer) -> None:
    with pytest.raises(InvalidTokenError, match="expired"):
        await _validator(issuer).validate(issuer.token(expires_in=-60))


async def test_token_without_expiry_rejected(issuer: FakeIssuer) -> None:
    # An unexpiring token is a standing key; `verify_exp` alone would let it
    # through, because it only checks an expiry that is actually present.
    with pytest.raises(InvalidTokenError):
        await _validator(issuer).validate(issuer.token(expires_in=None))


async def test_jwk_without_alg_uses_the_key_type_fallback(issuer: FakeIssuer) -> None:
    # `alg` is optional in RFC 7517. The validator derives RS256 from the key
    # type rather than trusting the token header.
    issuer.omit_alg = True
    claims = await _validator(issuer).validate(issuer.token(sub="carol"))
    assert claims.sub == "carol"


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
    # The classic confusion attack: sign with HMAC using the published public
    # key as the shared secret. Deriving the algorithm from the JWK (RS256)
    # rather than from the token header makes it fail.
    public_key_material = issuer.public_jwk()["n"]
    forged = issuer.token(algorithm="HS256", key=public_key_material)
    with pytest.raises(InvalidTokenError):
        await _validator(issuer).validate(forged)


async def test_caches_avoid_refetching(issuer: FakeIssuer) -> None:
    validator = _validator(issuer)
    for _ in range(3):
        await validator.validate(issuer.token())
    assert issuer.discovery_calls == 1
    assert issuer.jwks_calls == 1
