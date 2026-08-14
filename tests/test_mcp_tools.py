"""The MCP tool surface: mode downgrade rule and in-process tool calls."""

import json
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from catalog.schema import JobConfig
from mcp_app import ModeNotAllowedError, resolve_trigger_mode

from conftest import ApiHarness
from support import make_job

EXPECTED_TOOLS = {
    "list_ingest_jobs",
    "get_ingest_job",
    "trigger_reindex",
    "get_ingest_status",
    "list_ingest_runs",
    "get_ingest_run",
    "list_ingest_collections",
    "reload_ingest_config",
}


def _job(**overrides: Any) -> JobConfig:
    return JobConfig.model_validate(make_job(**overrides))


# ── the downgrade rule (pure) ─────────────────────────────────────────────────


def test_append_is_always_allowed() -> None:
    for mode in ("append", "upsert", "full"):
        job = _job(mode=mode)
        assert resolve_trigger_mode(job, "append") == "append"


def test_upsert_requires_upsert_or_full_job() -> None:
    assert resolve_trigger_mode(_job(mode="upsert"), "upsert") == "upsert"
    assert resolve_trigger_mode(_job(mode="full"), "upsert") == "upsert"
    with pytest.raises(ModeNotAllowedError, match="more destructive"):
        resolve_trigger_mode(_job(mode="append"), "upsert")


def test_full_requires_explicit_opt_in() -> None:
    with pytest.raises(ModeNotAllowedError, match="mcp_allow_full"):
        resolve_trigger_mode(_job(mode="full"), "full")
    allowed = _job(mode="full", mcp_allow_full=True)
    assert resolve_trigger_mode(allowed, "full") == "full"


def test_full_is_never_granted_for_collection_scope() -> None:
    job = _job(mode="full", mcp_allow_full=True, full_scope="collection")
    with pytest.raises(ModeNotAllowedError, match="mcp_allow_full"):
        resolve_trigger_mode(job, "full")


def test_default_mode_downgrades_a_closed_full_job() -> None:
    # No explicit request on a full job that has not opted in: run the next
    # less destructive mode rather than refusing outright.
    assert resolve_trigger_mode(_job(mode="full"), None) == "upsert"
    assert resolve_trigger_mode(_job(mode="full", mcp_allow_full=True), None) == "full"
    assert resolve_trigger_mode(_job(mode="append"), None) == "append"


# ── in-process tool calls ─────────────────────────────────────────────────────


def _payload(result: Any) -> dict[str, Any]:
    if getattr(result, "structured_content", None):
        return dict(result.structured_content)
    return dict(json.loads(result.content[0].text))


async def test_tool_roster_is_non_destructive(mcp_server: FastMCP) -> None:
    async with Client(mcp_server) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == EXPECTED_TOOLS
    # No tool may delete points, collections, orphans, or cancel runs.
    assert not any(
        word in name for name in names for word in ("delete", "remove", "cancel", "drop")
    )


async def test_list_jobs_and_status(api: ApiHarness, mcp_server: FastMCP) -> None:
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)
    async with Client(mcp_server) as client:
        jobs = _payload(await client.call_tool("list_ingest_jobs", {}))
        status = _payload(await client.call_tool("get_ingest_status", {}))
    assert jobs["count"] == 1
    assert jobs["jobs"][0]["id"] == "job-a"
    assert status["health"]["status"] == "ok"


async def test_get_job_redacts_secrets(api: ApiHarness, mcp_server: FastMCP) -> None:
    api.write_jobs_yaml(
        api.default_job(
            source={
                "type": "webdav",
                "label": "cloud",
                "url": "https://cloud.example.com/dav",
                "user": "svc",
                "pass": "${env:QI_SECRET_WEBDAV}",
            }
        )
    )
    api.engine.startup(fire_startup_runs=False)
    async with Client(mcp_server) as client:
        detail = _payload(await client.call_tool("get_ingest_job", {"job_id": "job-a"}))
    assert detail["config"]["source"]["pass"] == "***"


async def test_trigger_reindex_runs_and_reports(
    api: ApiHarness, mcp_server: FastMCP
) -> None:
    api.env.write_doc("a.md", "# A\n\nAlpha body.")
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)
    async with Client(mcp_server) as client:
        started = _payload(await client.call_tool("trigger_reindex", {"job_id": "job-a"}))
        assert started["status"] == "started"
        assert started["mode"] == "upsert"
        run = api.wait_run(started["run_id"])
        assert run.status == "success"
        assert run.trigger == "manual_mcp"

        detail = _payload(
            await client.call_tool("get_ingest_run", {"run_id": started["run_id"]})
        )
        assert detail["run"]["docs_indexed"] == 1
        runs = _payload(await client.call_tool("list_ingest_runs", {"job_id": "job-a"}))
        assert runs["count"] == 1


async def test_trigger_reindex_refuses_full_without_opt_in(
    api: ApiHarness, mcp_server: FastMCP
) -> None:
    api.env.write_doc("a.md", "# A\n\nAlpha body.")
    api.write_jobs_yaml(api.default_job(mode="full", mcp_allow_full=False))
    api.engine.startup(fire_startup_runs=False)
    async with Client(mcp_server) as client:
        result = _payload(
            await client.call_tool(
                "trigger_reindex", {"job_id": "job-a", "mode": "full"}
            )
        )
    assert result["status"] == "mode_not_allowed"
    assert "mcp_allow_full" in result["detail"]
    assert api.env.state.list_runs(job_id="job-a") == []  # nothing ran


async def test_trigger_reindex_allows_full_with_opt_in(
    api: ApiHarness, mcp_server: FastMCP
) -> None:
    api.env.write_doc("a.md", "# A\n\nAlpha body.")
    api.write_jobs_yaml(api.default_job(mode="full", mcp_allow_full=True))
    api.engine.startup(fire_startup_runs=False)
    async with Client(mcp_server) as client:
        result = _payload(
            await client.call_tool(
                "trigger_reindex", {"job_id": "job-a", "mode": "full"}
            )
        )
    assert result["status"] == "started"
    assert result["mode"] == "full"
    assert api.wait_run(result["run_id"]).status == "success"


async def test_unknown_job_is_an_error(api: ApiHarness, mcp_server: FastMCP) -> None:
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)
    async with Client(mcp_server) as client:
        with pytest.raises(Exception, match="unknown job"):
            await client.call_tool("get_ingest_job", {"job_id": "nope"})


async def test_collections_and_config_reload(
    api: ApiHarness, mcp_server: FastMCP
) -> None:
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)
    async with Client(mcp_server) as client:
        collections = _payload(await client.call_tool("list_ingest_collections", {}))
        assert collections["collections"][0]["collection"] == "col-a"
        config = _payload(await client.call_tool("reload_ingest_config", {}))
        assert config["valid"] is True
