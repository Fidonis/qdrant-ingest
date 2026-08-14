"""Collections listing and orphan cleanup."""

from conftest import AUTH, ApiHarness


def test_collections_listing_after_a_run(api: ApiHarness) -> None:
    api.env.write_doc("a.md", "# A\n\nAlpha body.")
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)
    run_id = api.client.post("/v1/jobs/job-a/run", headers=AUTH, json={}).json()["run_id"]
    api.wait_run(run_id)

    collections = api.client.get("/v1/collections", headers=AUTH).json()
    assert len(collections) == 1
    entry = collections[0]
    assert entry["collection"] == "col-a"
    assert entry["jobs"] == ["job-a"]
    assert entry["points"] > 0
    assert entry["meta"]["embedding_model"] == api.settings.embedding_model
    assert set(entry["indexes"]) >= {"source", "ingest_job", "ingest_run", "acl_tags"}


def test_renamed_job_becomes_an_orphan_and_can_be_deleted(api: ApiHarness) -> None:
    api.env.write_doc("a.md", "# A\n\nAlpha body.")
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)
    run_id = api.client.post("/v1/jobs/job-a/run", headers=AUTH, json={}).json()["run_id"]
    api.wait_run(run_id)

    # The job is renamed in jobs.yaml: its points and state rows stay behind.
    renamed = api.default_job(
        id="job-renamed",
        source={"type": "local", "label": "renamed", "path": str(api.env.docs_dir)},
    )
    api.write_jobs_yaml(renamed)
    api.client.post("/v1/config/reload", headers=AUTH)

    orphans = api.client.get("/v1/orphans", headers=AUTH).json()
    assert len(orphans) == 1
    assert orphans[0]["job_id"] == "job-a"
    assert orphans[0]["points"] > 0
    assert orphans[0]["state_rows"] == 1

    # Deletion needs explicit confirmation.
    assert (
        api.client.delete("/v1/orphans/job-a", headers=AUTH).status_code == 400
    )
    deleted = api.client.delete(
        "/v1/orphans/job-a", headers=AUTH, params={"confirm": "true"}
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted_rows"] == 1
    assert deleted.json()["deleted_points"] > 0
    assert api.client.get("/v1/orphans", headers=AUTH).json() == []
    assert api.env.qdrant.count("col-a").count == 0  # type: ignore[attr-defined]


def test_active_job_cannot_be_deleted_as_orphan(api: ApiHarness) -> None:
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)
    response = api.client.delete(
        "/v1/orphans/job-a", headers=AUTH, params={"confirm": "true"}
    )
    assert response.status_code == 409
