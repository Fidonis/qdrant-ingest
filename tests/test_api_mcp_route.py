"""How the MCP app is wired into the REST application.

The transport must answer the configured path itself. Mounting it instead
leaves that path to Starlette's redirect_slashes handling, which replies with a
307 to the trailing-slash form -- and MCP clients that guard against SSRF
refuse to follow a redirect whose target resolves to a private address, so they
abort before ever sending a bearer token.
"""

import json

import pytest
from fastapi.testclient import TestClient

from api.rest import create_app as create_rest_app
from mcp_app import OIDCValidator, build_mcp_app

from conftest import ApiHarness

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1.0"},
    },
}
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _client(api: ApiHarness, mcp_path: str) -> TestClient:
    """A REST app with the MCP app wired in, exactly as main.create_app does."""
    settings = api.settings.model_copy(update={"mcp_path": mcp_path})
    # The issuer is never contacted: every assertion here stops at the missing
    # bearer token, which the gate rejects before it looks at any key material.
    validator = OIDCValidator("https://issuer.test/realms/test", "test-audience")
    mcp_app = build_mcp_app(
        api.engine, validator, settings.oidc_operator_role, path=settings.mcp_path
    )
    app = create_rest_app(settings, api.engine, api.metrics, mcp_app)
    return TestClient(app)


@pytest.mark.parametrize("mcp_path", ["/mcp", "/ingest/mcp"])
def test_mcp_path_answers_without_redirect(api: ApiHarness, mcp_path: str) -> None:
    with _client(api, mcp_path) as client:
        response = client.post(
            mcp_path, headers=MCP_HEADERS, json=INITIALIZE, follow_redirects=False
        )

    assert response.status_code != 307, (
        f"POST {mcp_path} was redirected to "
        f"{response.headers.get('location')!r}; SSRF-guarded MCP clients do not "
        "follow that redirect"
    )
    # Reached the OIDC gate, which is what proves the transport was addressed.
    assert response.status_code == 401
    assert json.loads(response.content)["error"] == "missing_bearer_token"


def test_rest_surface_survives_the_mcp_route(api: ApiHarness) -> None:
    """The MCP route must not shadow the REST routes registered before it."""
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)

    with _client(api, "/mcp") as client:
        assert client.get("/health").status_code == 200
        assert client.get("/v1/jobs").status_code == 401
