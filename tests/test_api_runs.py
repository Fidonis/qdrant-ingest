"""Run endpoints: listing, details with events, cooperative cancel."""

import threading

from conftest import AUTH, ApiHarness


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
