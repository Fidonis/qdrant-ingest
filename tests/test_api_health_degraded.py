"""/health semantics: HTTP 200 always, cheap by construction, degraded on errors."""

import threading
import time

from conftest import ApiHarness


def test_missing_jobs_yaml_is_degraded_but_200(api: ApiHarness) -> None:
    # No jobs.yaml written: the container must come up cleanly regardless.
    api.engine.startup(fire_startup_runs=False)
    api.engine.refresh_deps()

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
    api.engine.refresh_deps()

    body = api.client.get("/health").json()
    assert body["status"] == "ok"
    assert body["jobs_loaded"] == 1
    assert body["config_error"] is None
    assert body["deps_checked_at"] is not None


def test_dep_failure_degrades(api: ApiHarness) -> None:
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)
    api.env.qdrant.raise_on_get_collections = ConnectionError("down")
    api.engine.refresh_deps()

    body = api.client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["deps"]["qdrant"] is False


def test_unprobed_dependencies_are_not_reported_as_down(api: ApiHarness) -> None:
    # Between process start and the first probe the dependencies are unknown.
    # Calling them down would degrade every fresh container for no reason.
    api.write_jobs_yaml(api.default_job())
    api.engine.reload_config(initial=True)

    body = api.client.get("/health").json()
    assert body["deps_checked_at"] is None
    assert body["status"] == "ok"


def test_health_does_not_probe_on_the_request_path(api: ApiHarness) -> None:
    """The regression this endpoint shipped with once.

    Probing inline made /health take as long as the slowest unreachable
    dependency, which is far longer than the container healthcheck allows —
    so a container that was serving perfectly well got marked unhealthy.
    """
    calls: list[str] = []

    def slow_probe() -> bool:
        calls.append("probed")
        time.sleep(5)
        return True

    api.engine._dep_probes["qdrant"] = slow_probe
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)

    started = time.monotonic()
    body = api.client.get("/health").json()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"/health took {elapsed:.2f}s — it probed inline"
    assert body["status"] in {"ok", "degraded"}


def test_health_stays_responsive_while_a_probe_hangs(api: ApiHarness) -> None:
    # The background refresh may block on an unreachable dependency; the
    # request path must not notice.
    release = threading.Event()

    def hanging_probe() -> bool:
        release.wait(timeout=10)
        return True

    api.engine._dep_probes["tika"] = hanging_probe
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)

    try:
        for _ in range(3):
            started = time.monotonic()
            assert api.client.get("/health").status_code == 200
            assert time.monotonic() - started < 1.0
    finally:
        release.set()
