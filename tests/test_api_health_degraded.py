"""/health semantics: HTTP 200 always, degraded status on config errors."""

from conftest import ApiHarness


def test_missing_jobs_yaml_is_degraded_but_200(api: ApiHarness) -> None:
    # No jobs.yaml written: the container must come up cleanly regardless.
    api.engine.startup(fire_startup_runs=False)

    response = api.client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["jobs_loaded"] == 0
    assert "not found" in body["config_error"]
    assert body["deps"] == {"qdrant": True, "embeddings": True, "tika": True}


def test_healthy_with_valid_catalog(api: ApiHarness) -> None:
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)

    body = api.client.get("/health").json()
    assert body["status"] == "ok"
    assert body["jobs_loaded"] == 1
    assert body["config_error"] is None


def test_dep_failure_degrades(api: ApiHarness) -> None:
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)
    api.env.qdrant.raise_on_get_collections = ConnectionError("down")

    body = api.client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["deps"]["qdrant"] is False
