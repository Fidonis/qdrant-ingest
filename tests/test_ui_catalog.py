"""Editing the job catalog through the web interface, end to end."""

from pathlib import Path

from catalog.writer import backup_path

from conftest import UiHarness


def _valid_catalog(ui: UiHarness) -> str:
    return f"""version: 1
jobs:
  - id: docs
    source:
      type: local
      label: docs
      path: {ui.env.docs_dir}
    target:
      collection: col-a
    mode: append
"""


def _job_form(ui: UiHarness, csrf: str, **overrides: str) -> dict[str, str]:
    form = {
        "csrf_token": csrf,
        "original_id": "",
        "id": "docs",
        "description": "Team documents",
        "enabled": "1",
        "source_type": "local",
        "source__label": "docs",
        "source__path": str(ui.env.docs_dir),
        "target__collection": "col-a",
        "target__acl_tags": "team:qa",
        "mode": "append",
        "chunking__strategy": "auto",
        "chunking__words": "400",
        "chunking__overlap": "50",
        "schedule__run_on_startup": "if_missed",
        "schedule__jitter_seconds": "30",
        "schedule__misfire_grace_seconds": "300",
        "safety__max_delete_ratio": "0.25",
        "safety__empty_source_guard": "1",
    }
    form.update(overrides)
    return form


# -- the raw editor ---------------------------------------------------------


def test_saving_the_raw_catalog_writes_and_reloads_it(ui: UiHarness) -> None:
    csrf = ui.login()
    response = ui.client.post(
        "/ui/catalog/save",
        data={"csrf_token": csrf, "raw": _valid_catalog(ui)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "col-a" in ui.jobs_path.read_text(encoding="utf-8")
    assert [job.id for job in ui.engine.jobs()] == ["docs"]


def test_the_raw_editor_preserves_comments(ui: UiHarness) -> None:
    csrf = ui.login()
    raw = "# why this job exists\n" + _valid_catalog(ui)
    ui.client.post("/ui/catalog/save", data={"csrf_token": csrf, "raw": raw})
    assert ui.jobs_path.read_text(encoding="utf-8").startswith("# why this job exists")


def test_an_invalid_catalog_is_reported_and_never_written(ui: UiHarness) -> None:
    csrf = ui.login()
    ui.client.post("/ui/catalog/save", data={"csrf_token": csrf, "raw": _valid_catalog(ui)})
    before = ui.jobs_path.read_text(encoding="utf-8")

    response = ui.client.post(
        "/ui/catalog/save",
        data={"csrf_token": csrf, "raw": _valid_catalog(ui).replace("append", "sideways")},
    )

    assert response.status_code == 422
    assert ui.jobs_path.read_text(encoding="utf-8") == before
    # The engine keeps serving the catalog that did load.
    assert [job.id for job in ui.engine.jobs()] == ["docs"]


def test_a_rejected_save_keeps_the_typed_text_in_the_form(ui: UiHarness) -> None:
    """Nobody should have to retype a long catalog because of one typo."""
    csrf = ui.login()
    response = ui.client.post(
        "/ui/catalog/save",
        data={"csrf_token": csrf, "raw": _valid_catalog(ui).replace("append", "sideways")},
    )
    assert "sideways" in response.text


# -- the form editor --------------------------------------------------------


def test_creating_a_job_through_the_form(ui: UiHarness) -> None:
    csrf = ui.login()
    response = ui.client.post(
        "/ui/jobs/save", data=_job_form(ui, csrf), follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/jobs/docs"
    assert [job.id for job in ui.engine.jobs()] == ["docs"]

    written = ui.jobs_path.read_text(encoding="utf-8")
    assert "col-a" in written
    assert "team:qa" in written


def test_the_form_omits_values_that_are_left_at_their_default(ui: UiHarness) -> None:
    """A job that changes nothing should not write forty lines of defaults."""
    csrf = ui.login()
    ui.client.post("/ui/jobs/save", data=_job_form(ui, csrf))
    written = ui.jobs_path.read_text(encoding="utf-8")
    assert "jitter_seconds" not in written
    assert "empty_source_guard" not in written
    assert "chunking" not in written


def test_editing_a_job_keeps_the_others(ui: UiHarness) -> None:
    csrf = ui.login()
    ui.client.post("/ui/jobs/save", data=_job_form(ui, csrf))
    ui.client.post(
        "/ui/jobs/save", data=_job_form(ui, csrf, id="other", source__label="other")
    )

    ui.client.post(
        "/ui/jobs/save",
        data=_job_form(ui, csrf, id="docs", original_id="docs", target__collection="col-b"),
    )

    assert sorted(job.id for job in ui.engine.jobs()) == ["docs", "other"]
    assert ui.engine.get_job("docs").target.collection == "col-b"
    assert ui.engine.get_job("other").target.collection == "col-a"


def test_renaming_a_job_keeps_its_position(ui: UiHarness) -> None:
    csrf = ui.login()
    ui.client.post("/ui/jobs/save", data=_job_form(ui, csrf, id="first"))
    ui.client.post(
        "/ui/jobs/save", data=_job_form(ui, csrf, id="second", source__label="second")
    )

    ui.client.post(
        "/ui/jobs/save", data=_job_form(ui, csrf, id="renamed", original_id="first")
    )

    assert [job.id for job in ui.engine.jobs()] == ["renamed", "second"]


def test_a_cross_job_collision_is_reported_by_the_form(ui: UiHarness) -> None:
    """Two jobs sharing a label and a collection would write the same URIs."""
    csrf = ui.login()
    ui.client.post("/ui/jobs/save", data=_job_form(ui, csrf))

    response = ui.client.post("/ui/jobs/save", data=_job_form(ui, csrf, id="second"))

    assert response.status_code == 422
    assert "labels must be unique per collection" in response.text
    assert [job.id for job in ui.engine.jobs()] == ["docs"]


def test_an_invalid_form_is_redisplayed_without_touching_the_file(ui: UiHarness) -> None:
    csrf = ui.login()
    ui.client.post("/ui/jobs/save", data=_job_form(ui, csrf))
    before = ui.jobs_path.read_text(encoding="utf-8")

    response = ui.client.post(
        "/ui/jobs/save",
        data=_job_form(ui, csrf, id="second", target__collection="not a valid collection!"),
    )

    assert response.status_code == 422
    assert ui.jobs_path.read_text(encoding="utf-8") == before


def test_a_form_without_an_id_is_refused(ui: UiHarness) -> None:
    csrf = ui.login()
    response = ui.client.post("/ui/jobs/save", data=_job_form(ui, csrf, id=""))
    assert response.status_code == 422
    assert "id is required" in response.text


def test_deleting_a_job_removes_it_from_the_catalog(ui: UiHarness) -> None:
    csrf = ui.login()
    ui.client.post("/ui/jobs/save", data=_job_form(ui, csrf))
    assert ui.jobs_path.exists()

    response = ui.client.post(
        "/ui/jobs/docs/delete", data={"csrf_token": csrf}, follow_redirects=False
    )

    assert response.status_code == 303
    assert ui.engine.jobs() == []
    assert "docs" not in ui.jobs_path.read_text(encoding="utf-8")


def test_a_write_keeps_the_previous_version_next_to_the_file(ui: UiHarness) -> None:
    csrf = ui.login()
    ui.client.post("/ui/jobs/save", data=_job_form(ui, csrf))
    ui.client.post(
        "/ui/jobs/save", data=_job_form(ui, csrf, id="other", source__label="other")
    )
    assert backup_path(ui.jobs_path).exists()


# -- pages that read the catalog -------------------------------------------


def test_the_job_editor_prefills_from_the_authored_document(ui: UiHarness) -> None:
    csrf = ui.login()
    ui.client.post("/ui/jobs/save", data=_job_form(ui, csrf))

    response = ui.client.get("/ui/jobs/docs/edit")

    assert response.status_code == 200
    assert "Team documents" in response.text
    assert "col-a" in response.text


def test_the_job_editor_is_404_for_an_unknown_job(ui: UiHarness) -> None:
    ui.login()
    assert ui.client.get("/ui/jobs/nope/edit").status_code == 404


def test_the_dashboard_surfaces_a_catalog_error(ui: UiHarness) -> None:
    """A broken catalog on disk must be visible, not silently empty."""
    ui.login()
    ui.write_catalog("version: 1\njobs: [\n")
    ui.engine.reload_config()

    response = ui.client.get("/ui/")

    assert response.status_code == 200
    assert "did not load cleanly" in response.text


# -- the legacy location ----------------------------------------------------


def test_a_legacy_catalog_is_read_only_and_offers_migration(ui: UiHarness) -> None:
    csrf = ui.login()
    legacy = Path(ui.settings.jobs_file_legacy)
    legacy.write_text(_valid_catalog(ui), encoding="utf-8")
    assert not ui.jobs_path.exists()

    page = ui.client.get("/ui/catalog")
    assert "old location" in page.text
    assert "readonly" in page.text

    response = ui.client.post(
        "/ui/catalog/migrate", data={"csrf_token": csrf}, follow_redirects=False
    )

    assert response.status_code == 303
    assert ui.jobs_path.exists()
    assert ui.client.get("/ui/catalog").text.count("old location") == 0
