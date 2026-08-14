"""Config endpoints: transactional reload semantics."""

from conftest import AUTH, ApiHarness


def test_valid_config_reports_checksum(api: ApiHarness) -> None:
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)

    config = api.client.get("/v1/config", headers=AUTH).json()
    assert config["valid"] is True
    assert config["errors"] == []
    assert config["checksum"] is not None
    assert config["applied"] is True


def test_broken_edit_keeps_the_previous_registry(api: ApiHarness) -> None:
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)
    assert len(api.client.get("/v1/jobs", headers=AUTH).json()) == 1

    # A typo lands in jobs.yaml: cron is invalid.
    api.write_jobs_yaml(api.default_job(schedule={"cron": "not a cron"}))
    reloaded = api.client.post("/v1/config/reload", headers=AUTH).json()
    assert reloaded["valid"] is False
    assert reloaded["applied"] is False
    assert any(err["field"] == "schedule.cron" for err in reloaded["errors"])

    # The previous registry keeps serving.
    jobs = api.client.get("/v1/jobs", headers=AUTH).json()
    assert len(jobs) == 1
    health = api.client.get("/health").json()
    assert health["status"] == "degraded"
    assert "cron" in health["config_error"]


def test_fixed_edit_applies_again(api: ApiHarness) -> None:
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)
    api.write_jobs_yaml(api.default_job(schedule={"cron": "not a cron"}))
    api.client.post("/v1/config/reload", headers=AUTH)

    second_job = api.default_job(
        id="job-b",
        source={"type": "local", "label": "other", "path": str(api.env.docs_dir)},
        target={"collection": "col-b"},
    )
    api.write_jobs_yaml(api.default_job(), second_job)
    reloaded = api.client.post("/v1/config/reload", headers=AUTH).json()
    assert reloaded["valid"] is True
    assert reloaded["applied"] is True
    assert len(api.client.get("/v1/jobs", headers=AUTH).json()) == 2
