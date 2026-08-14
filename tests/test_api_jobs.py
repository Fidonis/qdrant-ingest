"""Job endpoints: listing, redaction, triggering, pause, preview."""

import threading

from conftest import AUTH, ApiHarness


def test_job_listing_and_detail_with_redaction(api: ApiHarness) -> None:
    webdav_job = api.default_job(
        id="dav-job",
        source={
            "type": "webdav",
            "label": "cloud",
            "url": "https://cloud.example.com/dav",
            "user": "svc",
            "pass": "${env:QI_SECRET_WEBDAV}",
        },
        target={"collection": "col-dav"},
    )
    api.write_jobs_yaml(api.default_job(), webdav_job)
    api.engine.startup(fire_startup_runs=False)

    listing = api.client.get("/v1/jobs", headers=AUTH).json()
    assert {entry["id"] for entry in listing} == {"job-a", "dav-job"}

    detail = api.client.get("/v1/jobs/dav-job", headers=AUTH).json()
    assert detail["config"]["source"]["pass"] == "***"  # secret redacted
    assert detail["config"]["source"]["user"] == "svc"

    assert api.client.get("/v1/jobs/unknown", headers=AUTH).status_code == 404


def test_trigger_run_and_history(api: ApiHarness) -> None:
    api.env.write_doc("a.md", "# A\n\nAlpha body.")
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)

    response = api.client.post("/v1/jobs/job-a/run", headers=AUTH, json={})
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    run = api.wait_run(run_id)
    assert run.status == "success"
    assert run.trigger == "manual_rest"

    detail = api.client.get("/v1/jobs/job-a", headers=AUTH).json()
    assert detail["runs"][0]["run_id"] == run_id


def test_dry_run_flag_reaches_the_engine(api: ApiHarness) -> None:
    api.env.write_doc("a.md", "# A\n\nAlpha body.")
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)

    response = api.client.post(
        "/v1/jobs/job-a/run", headers=AUTH, json={"dry_run": True}
    )
    run = api.wait_run(response.json()["run_id"])
    assert run.status == "success"
    assert api.env.qdrant.point_count("col-a") == 0


def test_concurrent_trigger_conflicts_and_queue(api: ApiHarness) -> None:
    api.env.write_doc("a.md", "# A\n\nAlpha body.")
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)

    # Gate the embedder so the first run holds the job lock.
    entered = threading.Event()
    release = threading.Event()
    inner = api.env.embeddings.embed_all

    def gated(texts: list[str], batch_size: int) -> list[list[float]]:
        entered.set()
        assert release.wait(timeout=10)
        return inner(texts, batch_size)

    api.env.embeddings.embed_all = gated  # type: ignore[method-assign]

    first = api.client.post("/v1/jobs/job-a/run", headers=AUTH, json={})
    assert first.status_code == 202
    assert entered.wait(timeout=10)

    conflict = api.client.post("/v1/jobs/job-a/run", headers=AUTH, json={})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == "already_running"

    queued = api.client.post("/v1/jobs/job-a/run", headers=AUTH, json={"queue": True})
    assert queued.status_code == 202
    assert queued.json()["queued"] is True

    release.set()
    first_run = api.wait_run(first.json()["run_id"])
    assert first_run.status == "success"
    # The queued follow-up run fires after the first finishes.
    for _ in range(200):
        runs = api.env.state.list_runs(job_id="job-a", limit=10)
        if len(runs) >= 2 and all(r.status != "running" for r in runs):
            break
        import time

        time.sleep(0.02)
    runs = api.env.state.list_runs(job_id="job-a", limit=10)
    assert len(runs) == 2


def test_pause_and_resume(api: ApiHarness) -> None:
    api.write_jobs_yaml(api.default_job(schedule={"cron": "0 2 * * *"}))
    api.engine.startup(fire_startup_runs=False)

    paused = api.client.post("/v1/jobs/job-a/pause", headers=AUTH)
    assert paused.status_code == 200
    assert api.client.get("/v1/jobs", headers=AUTH).json()[0]["paused"] is True

    resumed = api.client.post("/v1/jobs/job-a/resume", headers=AUTH)
    assert resumed.status_code == 200
    assert api.client.get("/v1/jobs", headers=AUTH).json()[0]["paused"] is False


def test_preview_lists_scan_candidates(api: ApiHarness) -> None:
    api.env.write_doc("keep/a.md", "# A\n\nBody.")
    api.env.write_doc("keep/b.tmp", "junk")
    job = api.default_job(filters={"include": ["**/*.md"]})
    api.write_jobs_yaml(job)
    api.engine.startup(fire_startup_runs=False)

    preview = api.client.get("/v1/jobs/job-a/preview", headers=AUTH).json()
    assert preview["count"] == 1
    assert preview["files"][0]["rel_path"] == "keep/a.md"
    assert preview["files"][0]["source"] == "local://docs/keep/a.md"
