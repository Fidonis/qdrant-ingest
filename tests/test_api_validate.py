"""POST /v1/config/validate: would this catalog load? Answered without writing anything."""

from typing import Any

import yaml

from conftest import AUTH, ApiHarness


def _catalog(api: ApiHarness, *jobs: dict[str, Any]) -> str:
    return yaml.safe_dump(
        {"version": 1, "defaults": {"embedding": {"model": "test-model"}}, "jobs": list(jobs)}
    )


def _validate(api: ApiHarness, raw: str) -> dict[str, Any]:
    response = api.client.post("/v1/config/validate", headers=AUTH, json={"raw": raw})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_a_valid_catalog_is_ok(api: ApiHarness) -> None:
    result = _validate(api, _catalog(api, api.default_job()))

    assert result == {"ok": True, "errors": [], "jobs": 1}


def test_problems_come_back_with_job_and_field(api: ApiHarness) -> None:
    api.engine.startup(fire_startup_runs=False)
    bad = api.default_job(
        id="bad",
        schedule={"cron": "not a cron"},
        target={"collection": "col-b", "connection": "nowhere"},
        source={
            "type": "webdav",
            "label": "cloud",
            "url": "https://cloud.test/dav",
            "pass": "${env:QI_SECRET_MISSING}",
        },
    )

    result = _validate(api, _catalog(api, api.default_job(), bad))

    assert result["ok"] is False
    by_field = {(e["job_id"], e["field"]): e["message"] for e in result["errors"]}
    assert "invalid cron expression" in by_field[("bad", "schedule.cron")]
    assert "unknown connection 'nowhere'" in by_field[("bad", "target.connection")]
    assert "QI_SECRET_MISSING" in by_field[("bad", "source.password")]
    assert result["jobs"] == 1  # the good job loaded, the bad one did not


def test_cross_job_rules_apply(api: ApiHarness) -> None:
    twin = api.default_job(id="twin")  # same collection and label as job-a

    result = _validate(api, _catalog(api, api.default_job(), twin))

    assert result["ok"] is False
    assert {e["field"] for e in result["errors"]} == {"source.label"}


def test_text_that_is_not_a_catalog_is_reported_not_raised(api: ApiHarness) -> None:
    result = _validate(api, "jobs: [unterminated")

    assert result["ok"] is False
    assert result["errors"][0]["job_id"] is None
    assert result["errors"][0]["field"] == "jobs_file"
    assert result["jobs"] == 0


def test_validating_writes_nothing_and_leaves_the_running_catalog_alone(
    api: ApiHarness,
) -> None:
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)
    before = api.jobs_path.read_bytes()

    _validate(api, _catalog(api))  # an empty catalog, valid, would unload job-a if applied
    _validate(api, "not: [valid")

    assert api.jobs_path.read_bytes() == before
    assert [p.name for p in api.jobs_path.parent.iterdir()] == ["jobs.yaml"]
    assert [job["id"] for job in api.client.get("/v1/jobs", headers=AUTH).json()] == ["job-a"]


def test_the_answer_is_the_one_a_reload_of_that_file_gives(api: ApiHarness) -> None:
    candidate = _catalog(
        api,
        api.default_job(id="a"),
        api.default_job(id="b", schedule={"cron": "nope"}),
    )
    predicted = _validate(api, candidate)

    api.jobs_path.parent.mkdir(parents=True, exist_ok=True)
    api.jobs_path.write_text(candidate, encoding="utf-8")
    reloaded = api.client.post("/v1/config/reload", headers=AUTH).json()

    assert predicted["errors"] == reloaded["errors"]
    assert predicted["ok"] is reloaded["valid"] is False


def test_the_endpoint_is_guarded_and_strict(api: ApiHarness) -> None:
    assert api.client.post("/v1/config/validate", json={"raw": ""}).status_code == 401
    for body in ({}, {"raw": 5}, {"raw": "", "extra": 1}):
        response = api.client.post("/v1/config/validate", headers=AUTH, json=body)
        assert response.status_code == 422, body
