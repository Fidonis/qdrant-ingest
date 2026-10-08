"""Run endpoints: listing, details with events, cooperative cancel, deleting history."""

import threading

from conftest import AUTH, ApiHarness
from support import make_run


def _boot_with_doc(api: ApiHarness) -> None:
    api.env.write_doc("a.md", "# A\n\nAlpha body.")
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)


def test_list_runs_and_details(api: ApiHarness) -> None:
    _boot_with_doc(api)
    run_id = api.client.post("/v1/jobs/job-a/run", headers=AUTH, json={}).json()["run_id"]
    api.wait_run(run_id)

    runs = api.client.get("/v1/runs", headers=AUTH).json()
    assert [run["run_id"] for run in runs] == [run_id]
    filtered = api.client.get(
        "/v1/runs", headers=AUTH, params={"job_id": "job-a", "status": "success"}
    ).json()
    assert len(filtered) == 1

    detail = api.client.get(f"/v1/runs/{run_id}", headers=AUTH).json()
    assert detail["run"]["status"] == "success"
    assert isinstance(detail["events"], list)

    assert api.client.get("/v1/runs/nope", headers=AUTH).status_code == 404


def test_cancel_running_run(api: ApiHarness) -> None:
    for i in range(5):
        api.env.write_doc(f"doc{i}.md", f"# D{i}\n\nBody {i}.")
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)

    # Gate after the first document so the run is provably in flight.
    entered = threading.Event()
    release = threading.Event()
    inner = api.env.embeddings.embed_all
    calls = {"n": 0}

    def gated(texts: list[str], batch_size: int) -> list[list[float]]:
        calls["n"] += 1
        if calls["n"] == 2:
            entered.set()
            assert release.wait(timeout=10)
        return inner(texts, batch_size)

    api.env.embeddings.embed_all = gated  # type: ignore[method-assign]

    run_id = api.client.post("/v1/jobs/job-a/run", headers=AUTH, json={}).json()["run_id"]
    assert entered.wait(timeout=10)

    cancel = api.client.delete(f"/v1/runs/{run_id}", headers=AUTH)
    assert cancel.status_code == 202
    release.set()

    run = api.wait_run(run_id)
    assert run.status == "interrupted"

    # Cancelling a finished run conflicts.
    assert api.client.delete(f"/v1/runs/{run_id}", headers=AUTH).status_code == 409


def _seed_runs(api: ApiHarness) -> None:
    """Finished runs of job-a on three days, one of a job that is not in the catalog."""
    _boot_with_doc(api)
    for run_id, job_id, day in (
        ("a1", "job-a", "01"), ("a2", "job-a", "02"), ("a3", "job-a", "03"),
        ("g1", "gone", "02"),
    ):
        api.env.state.create_run(
            make_run(
                run_id=run_id,
                job_id=job_id,
                status="success",
                started_at=f"2026-08-{day}T12:00:00+00:00",
            )
        )
        api.env.state.add_event(run_id, "info", f"event of {run_id}")


def test_delete_runs_needs_a_confirmation_or_a_dry_run(api: ApiHarness) -> None:
    _seed_runs(api)

    refused = api.client.delete("/v1/jobs/job-a/runs", headers=AUTH)

    assert refused.status_code == 400
    assert len(api.env.state.list_runs(job_id="job-a")) == 3


def test_delete_runs_requires_the_token(api: ApiHarness) -> None:
    _seed_runs(api)

    response = api.client.delete("/v1/jobs/job-a/runs", params={"confirm": "true"})

    assert response.status_code == 401
    assert len(api.env.state.list_runs(job_id="job-a")) == 3


def test_delete_runs_dry_run_counts_without_deleting(api: ApiHarness) -> None:
    _seed_runs(api)

    body = api.client.delete(
        "/v1/jobs/job-a/runs", headers=AUTH, params={"dry_run": "true"}
    ).json()

    assert body == {
        "matched": 3, "matched_events": 3, "deleted_runs": 0, "deleted_events": 0,
        "skipped_running": 0, "dry_run": True,
    }
    assert len(api.env.state.list_runs(job_id="job-a")) == 3


def test_delete_runs_in_a_range_accepts_z_and_naive_instants(api: ApiHarness) -> None:
    _seed_runs(api)

    # 2026-08-02 12:00 is included, 2026-08-03 12:00 is not: "Z" and no offset both mean UTC.
    body = api.client.delete(
        "/v1/jobs/job-a/runs",
        headers=AUTH,
        params={"since": "2026-08-02T12:00:00Z", "until": "2026-08-03T12:00:00", "confirm": "true"},
    ).json()

    assert body["deleted_runs"] == 1
    assert body["deleted_events"] == 1
    assert body["dry_run"] is False
    remaining = [run.run_id for run in api.env.state.list_runs(job_id="job-a")]
    assert remaining == ["a3", "a1"]


def test_delete_runs_works_for_a_job_that_is_not_in_the_catalog(api: ApiHarness) -> None:
    _seed_runs(api)

    body = api.client.delete(
        "/v1/jobs/gone/runs", headers=AUTH, params={"confirm": "true"}
    ).json()

    assert body["deleted_runs"] == 1
    assert api.env.state.list_runs(job_id="gone") == []
    assert len(api.env.state.list_runs(job_id="job-a")) == 3


def test_delete_runs_skips_a_run_that_is_working(api: ApiHarness) -> None:
    _seed_runs(api)
    api.env.state.create_run(
        make_run(
            run_id="live",
            job_id="job-a",
            status="running",
            started_at="2026-08-04T00:00:00+00:00",
        )
    )

    body = api.client.delete(
        "/v1/jobs/job-a/runs", headers=AUTH, params={"confirm": "true"}
    ).json()

    assert body["deleted_runs"] == 3
    assert body["skipped_running"] == 1
    assert [run.run_id for run in api.env.state.list_runs(job_id="job-a")] == ["live"]


def test_delete_runs_refuses_a_bad_range(api: ApiHarness) -> None:
    _seed_runs(api)

    not_a_date = api.client.delete(
        "/v1/jobs/job-a/runs", headers=AUTH, params={"since": "yesterday", "confirm": "true"}
    )
    reversed_range = api.client.delete(
        "/v1/jobs/job-a/runs",
        headers=AUTH,
        params={"since": "2026-08-03", "until": "2026-08-01", "confirm": "true"},
    )

    assert not_a_date.status_code == 422
    assert reversed_range.status_code == 422
    assert len(api.env.state.list_runs(job_id="job-a")) == 3


def test_cancel_route_is_still_the_cancel_route(api: ApiHarness) -> None:
    # DELETE /v1/runs/{id} cancels a working run; deleting history lives under the job.
    _seed_runs(api)

    assert api.client.delete("/v1/runs/a1", headers=AUTH).status_code == 409
    assert len(api.env.state.list_runs(job_id="job-a")) == 3
