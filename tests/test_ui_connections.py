"""Managing database connections through the web interface, end to end."""

from pathlib import Path

import pytest

from connections.loader import load_connections

from conftest import UiHarness


def _conn_form(csrf: str, **overrides: str) -> dict[str, str]:
    form = {
        "csrf_token": csrf,
        "original_name": "",
        "name": "research",
        "url": "http://research.qdrant:6333",
    }
    form.update(overrides)
    return form


def _connections_on_disk(ui: UiHarness) -> list[str]:
    result = load_connections(Path(ui.settings.connections_file), ui.settings)
    return sorted(result.names())


def test_the_list_page_shows_the_seeded_connection(ui: UiHarness) -> None:
    ui.login()
    page = ui.client.get("/ui/connections")
    assert page.status_code == 200
    assert "db-a" in page.text


def test_creating_a_connection(ui: UiHarness) -> None:
    csrf = ui.login()
    response = ui.client.post(
        "/ui/connections/save", data=_conn_form(csrf), follow_redirects=False
    )
    assert response.status_code == 303
    assert _connections_on_disk(ui) == ["db-a", "research"]
    assert "research" in ui.engine.connection_names()


def test_a_bad_url_is_rejected_and_nothing_is_written(ui: UiHarness) -> None:
    csrf = ui.login()
    before = Path(ui.settings.connections_file).read_text(encoding="utf-8")
    response = ui.client.post(
        "/ui/connections/save",
        data=_conn_form(csrf, url="research.qdrant:6333"),
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert Path(ui.settings.connections_file).read_text(encoding="utf-8") == before


def test_an_api_key_is_stored_encrypted_never_in_the_clear(ui: UiHarness) -> None:
    csrf = ui.login()
    ui.client.post("/ui/connections/save", data=_conn_form(csrf, api_key="super-secret-key"))

    on_disk = Path(ui.settings.connections_file).read_text(encoding="utf-8")
    assert "super-secret-key" not in on_disk
    assert "enc:1:" in on_disk

    # ... and it round-trips back to the plaintext for use.
    result = load_connections(Path(ui.settings.connections_file), ui.settings)
    research = next(c for c in result.connections if c.name == "research")
    assert research.api_key == "super-secret-key"


def test_editing_keeps_the_stored_key_when_the_field_is_blank(ui: UiHarness) -> None:
    csrf = ui.login()
    ui.client.post("/ui/connections/save", data=_conn_form(csrf, api_key="k1"))

    ui.client.post(
        "/ui/connections/save",
        data=_conn_form(csrf, original_name="research", url="http://moved:6333"),
    )
    result = load_connections(Path(ui.settings.connections_file), ui.settings)
    research = next(c for c in result.connections if c.name == "research")
    assert research.url == "http://moved:6333"
    assert research.api_key == "k1"  # kept


def test_a_connection_in_use_by_a_job_cannot_be_deleted(ui: UiHarness) -> None:
    csrf = ui.login()
    ui.write_catalog(
        "version: 1\n"
        "defaults: {embedding: {model: m}}\n"
        "jobs:\n"
        "  - id: j\n"
        f"    source: {{type: local, label: j, path: {ui.env.docs_dir}}}\n"
        "    target: {collection: c, connection: db-a}\n"
        "    mode: append\n"
    )
    ui.engine.reload_config()

    response = ui.client.post(
        "/ui/connections/db-a/delete", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "db-a" in _connections_on_disk(ui)  # still there


def test_deleting_an_unused_connection(ui: UiHarness) -> None:
    csrf = ui.login()
    ui.client.post("/ui/connections/save", data=_conn_form(csrf))
    response = ui.client.post(
        "/ui/connections/research/delete", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert response.status_code == 303
    assert _connections_on_disk(ui) == ["db-a"]


def test_the_test_button_reports_success(ui: UiHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    csrf = ui.login()
    monkeypatch.setattr(
        "ui.routes.probe_connection", lambda *a, **k: "reachable — 3 collection(s)"
    )
    response = ui.client.post(
        "/ui/connections/test",
        data={"csrf_token": csrf, "url": "http://qdrant:6333"},
    )
    assert response.status_code == 200
    assert "reachable — 3 collection(s)" in response.text


def test_the_test_button_reports_failure(ui: UiHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    csrf = ui.login()

    def _boom(*_a: object, **_k: object) -> str:
        raise ConnectionError("name or service not known")

    monkeypatch.setattr("ui.routes.probe_connection", _boom)
    response = ui.client.post(
        "/ui/connections/test",
        data={"csrf_token": csrf, "url": "http://nope:6333"},
    )
    assert response.status_code == 200
    assert "ConnectionError" in response.text
