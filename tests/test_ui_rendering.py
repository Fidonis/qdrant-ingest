"""Pages rendered against real run and job data.

The access and catalog-editing tests never exercise a populated run: every
fixture there starts with an empty catalog and no history, so the `{% for run
in runs %}` loop bodies in the templates -- and every filter/field access
inside them -- never actually ran. That gap let field names invented from
memory (RunRow has no `duration_seconds`; RunEvent's timestamp is `ts`, not
`at`; RunStatus is `running|success|failed|interrupted|aborted_guard|
aborted_lock`, not `ok|partial|aborted`) sit in the templates undetected.
These tests close it by triggering a real run and rendering every page that
touches its data.
"""

import time
from typing import get_args

from state import RunRow, now_iso
from state.models import RunStatus

from conftest import UiHarness


def _catalog(ui: UiHarness) -> str:
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


def _run_a_real_job(ui: UiHarness) -> str:
    """Trigger a real local-source run through the engine and wait for it."""
    ui.env.write_doc("a.md", "# A\n\nAlpha body.")
    ui.write_catalog(_catalog(ui))
    ui.engine.reload_config()
    result = ui.engine.trigger_run("docs", "manual_ui")
    run_id = result["run_id"]

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        run = ui.env.state.get_run(run_id)
        if run is not None and run.status != "running":
            break
        time.sleep(0.02)
    else:
        raise AssertionError(f"run {run_id} did not finish within 15s")
    return run_id


def test_the_dashboard_renders_a_finished_run(ui: UiHarness) -> None:
    _run_a_real_job(ui)
    ui.login()

    response = ui.client.get("/ui/")

    assert response.status_code == 200
    assert "success" in response.text
    assert "docs" in response.text


def test_the_runs_page_renders_a_finished_run(ui: UiHarness) -> None:
    _run_a_real_job(ui)
    ui.login()

    response = ui.client.get("/ui/runs")

    assert response.status_code == 200
    assert "success" in response.text


def test_the_job_detail_page_renders_its_run_history(ui: UiHarness) -> None:
    _run_a_real_job(ui)
    ui.login()

    response = ui.client.get("/ui/jobs/docs")

    assert response.status_code == 200
    assert "success" in response.text


def test_the_run_detail_page_renders_counters_and_events(ui: UiHarness) -> None:
    run_id = _run_a_real_job(ui)
    ui.login()

    response = ui.client.get(f"/ui/runs/{run_id}")

    assert response.status_code == 200
    assert "success" in response.text
    # docs_indexed on a one-document local job: exactly one file went in.
    assert ">1<" in response.text


def test_a_run_still_in_progress_shows_an_elapsed_duration_without_crashing(
    ui: UiHarness,
) -> None:
    """The duration filter must handle a run with no finished_at yet."""
    ui.env.state.create_run(
        RunRow(
            run_id="run-in-progress",
            job_id="docs",
            mode="append",
            trigger="manual_ui",
            started_at=now_iso(),
            status="running",
        )
    )
    ui.login()

    response = ui.client.get("/ui/runs")

    assert response.status_code == 200


def test_status_colours_cover_every_real_run_status(ui: UiHarness) -> None:
    """Every RunStatus literal must render, not silently fall through."""
    for status in get_args(RunStatus):
        ui.env.state.create_run(
            RunRow(
                run_id=f"run-{status}",
                job_id="docs",
                mode="append",
                trigger="manual_ui",
                started_at="2026-01-01T00:00:00+00:00",
                finished_at="2026-01-01T00:00:05+00:00",
                status=status,
            )
        )
    ui.login()

    response = ui.client.get("/ui/runs")

    assert response.status_code == 200
    for status in get_args(RunStatus):
        assert status in response.text
