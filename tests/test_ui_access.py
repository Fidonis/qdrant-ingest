"""Who gets into the web interface, and what a session does not buy.

The last two tests are the load-bearing ones: a browser session is a way into
/ui and nothing else. If a session ever became a way into /v1 or /mcp, the
token and OIDC guards on those planes would be decoration.
"""

import time

from conftest import API_TOKEN, UiHarness


def test_anonymous_request_is_sent_to_the_login_route(ui: UiHarness) -> None:
    response = ui.client.get("/ui/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/auth/login"


def test_anonymous_htmx_request_gets_a_client_side_redirect(ui: UiHarness) -> None:
    """A redirect inside a swap would render the login page into a panel."""
    response = ui.client.get("/ui/partials/health", headers={"HX-Request": "true"})
    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == "/ui/auth/login"


def test_a_session_without_the_operator_role_is_refused(ui: UiHarness) -> None:
    ui.login(roles=("some-other-role",))
    response = ui.client.get("/ui/")
    assert response.status_code == 403
    assert "qdrant-ingest-operator" in response.text


def test_an_operator_reaches_every_page(ui: UiHarness) -> None:
    ui.login()
    for path in (
        "/ui/",
        "/ui/jobs",
        "/ui/jobs/new",
        "/ui/runs",
        "/ui/collections",
        "/ui/orphans",
        "/ui/catalog",
    ):
        assert ui.client.get(path).status_code == 200, path


def test_an_expired_session_is_sent_back_to_the_login_route(ui: UiHarness) -> None:
    ui.client.cookies.set(
        "qdrant_ingest_ui",
        ui.sign_session(
            {
                "user": {
                    "sub": "user-1",
                    "username": "tester",
                    "roles": ["qdrant-ingest-operator"],
                    "expires_at": int(time.time()) - 1,
                },
                "csrf": "x",
            }
        ),
    )
    response = ui.client.get("/ui/", follow_redirects=False)
    assert response.status_code == 303


def test_an_unsigned_cookie_is_not_a_session(ui: UiHarness) -> None:
    ui.client.cookies.set("qdrant_ingest_ui", "not-a-signed-value")
    response = ui.client.get("/ui/", follow_redirects=False)
    assert response.status_code == 303


def test_a_mutation_without_a_csrf_token_is_refused(ui: UiHarness) -> None:
    ui.login()
    response = ui.client.post("/ui/catalog/reload", data={})
    assert response.status_code == 403


def test_a_mutation_with_a_wrong_csrf_token_is_refused(ui: UiHarness) -> None:
    ui.login()
    response = ui.client.post("/ui/catalog/reload", data={"csrf_token": "wrong"})
    assert response.status_code == 403


def test_a_mutation_with_the_session_csrf_token_is_accepted(ui: UiHarness) -> None:
    csrf = ui.login()
    response = ui.client.post(
        "/ui/catalog/reload", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert response.status_code == 303


def test_the_login_callback_rejects_a_mismatched_state(ui: UiHarness) -> None:
    """Without the stored state the callback cannot be replayed from a link."""
    response = ui.client.get("/ui/auth/callback?code=abc&state=forged")
    assert response.status_code == 400


# -- the interface is not a way into the other two planes --------------------


def test_a_session_grants_no_access_to_the_rest_plane(ui: UiHarness) -> None:
    ui.login()
    assert ui.client.get("/v1/jobs").status_code == 401
    # ... while the token still works, from the same client.
    with_token = ui.client.get("/v1/jobs", headers={"Authorization": f"Bearer {API_TOKEN}"})
    assert with_token.status_code == 200


def test_a_session_grants_no_access_to_the_mcp_endpoint(ui: UiHarness) -> None:
    ui.login()
    response = ui.client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
    assert response.status_code in (401, 403)


def test_the_health_route_stays_open(ui: UiHarness) -> None:
    """The container healthcheck must not need a session or a token."""
    ui.logout()
    assert ui.client.get("/health").status_code == 200


# -- an unreachable identity provider ---------------------------------------


def test_the_login_route_reports_an_unreachable_provider(ui: UiHarness) -> None:
    """A wrong issuer URL is the likeliest first-run mistake.

    The discovery fetch fails at the transport layer, which is not a token
    problem and must not surface as a stack trace: the operator needs to be
    told the provider could not be reached.
    """
    response = ui.client.get("/ui/auth/login", follow_redirects=False)
    assert response.status_code == 502
    assert "identity provider unreachable" in response.text
