"""Bearer authentication on the REST surface."""

from conftest import AUTH, ApiHarness


def _boot(api: ApiHarness) -> None:
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)


def test_v1_requires_token(api: ApiHarness) -> None:
    _boot(api)
    response = api.client.get("/v1/jobs")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_wrong_token_rejected(api: ApiHarness) -> None:
    _boot(api)
    response = api.client.get("/v1/jobs", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_health_is_free(api: ApiHarness) -> None:
    _boot(api)
    assert api.client.get("/health").status_code == 200


def test_metrics_requires_token_by_default(api: ApiHarness) -> None:
    _boot(api)
    assert api.client.get("/metrics").status_code == 401
    ok = api.client.get("/metrics", headers=AUTH)
    assert ok.status_code == 200
    assert b"qdrant_ingest_jobs_loaded" in ok.content


def test_correct_token_grants_access(api: ApiHarness) -> None:
    _boot(api)
    response = api.client.get("/v1/jobs", headers=AUTH)
    assert response.status_code == 200
    assert response.json()[0]["id"] == "job-a"
