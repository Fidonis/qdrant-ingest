"""GET /v1/jobs/{id}/documents: what happened to each file of a job."""

from typing import Any

from conftest import AUTH, ApiHarness
from support import make_document


def _seed(api: ApiHarness) -> None:
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)
    rows: list[dict[str, Any]] = [
        {"rel_path": "a/report.pdf", "status": "indexed", "chunk_count": 4, "last_run_id": "r1"},
        {"rel_path": "a/notes_v2.md", "status": "indexed", "chunk_count": 2, "last_run_id": "r2"},
        {"rel_path": "b/100%.md", "status": "indexed", "chunk_count": 1, "last_run_id": "r2"},
        {
            "rel_path": "b/scan.pdf",
            "status": "skipped_no_text",
            "last_error": None,
            "last_run_id": "r2",
        },
        {
            "rel_path": "b/broken.docx",
            "status": "failed_extract",
            "last_error": "Tika answered 500",
            "last_run_id": "r2",
        },
    ]
    for index, row in enumerate(rows):
        api.env.state.upsert_document(
            make_document(
                source=f"local://docs/{row['rel_path']}",
                indexed_at=f"2026-10-0{index + 1}T10:00:00+00:00",
                **row,
            )
        )
    # Another job's documents never show up.
    api.env.state.upsert_document(
        make_document(job_id="other", source="local://other/x.md", rel_path="x.md")
    )


def _get(api: ApiHarness, **params: Any) -> dict[str, Any]:
    response = api.client.get("/v1/jobs/job-a/documents", headers=AUTH, params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_lists_the_documents_of_one_job_with_counts_over_all_of_them(api: ApiHarness) -> None:
    _seed(api)

    body = _get(api)

    assert body["total"] == 5
    assert body["counts"] == {"indexed": 3, "skipped_no_text": 1, "failed_extract": 1}
    assert [item["rel_path"] for item in body["items"]] == [
        "a/notes_v2.md",
        "a/report.pdf",
        "b/100%.md",
        "b/broken.docx",
        "b/scan.pdf",
    ]
    failed = next(item for item in body["items"] if item["status"] == "failed_extract")
    assert failed["last_error"] == "Tika answered 500"
    assert failed["source"] == "local://docs/b/broken.docx"


def test_a_status_filter_narrows_the_page_but_not_the_counts(api: ApiHarness) -> None:
    _seed(api)

    body = _get(api, status="failed_extract")

    assert body["total"] == 1
    assert [item["rel_path"] for item in body["items"]] == ["b/broken.docx"]
    assert body["counts"]["indexed"] == 3


def test_a_search_matches_the_path_and_treats_like_characters_literally(
    api: ApiHarness,
) -> None:
    _seed(api)

    assert [i["rel_path"] for i in _get(api, q="REPORT")["items"]] == ["a/report.pdf"]
    assert [i["rel_path"] for i in _get(api, q="100%")["items"]] == ["b/100%.md"]
    # `_` is a LIKE wildcard; here it must only match an underscore.
    assert [i["rel_path"] for i in _get(api, q="notes_v")["items"]] == ["a/notes_v2.md"]
    assert _get(api, q="notes.v")["items"] == []
    assert _get(api, q="%")["total"] == 1


def test_paging_and_order(api: ApiHarness) -> None:
    _seed(api)

    first = _get(api, limit=2, offset=0)
    second = _get(api, limit=2, offset=2)

    assert first["total"] == second["total"] == 5
    assert [i["rel_path"] for i in first["items"]] == ["a/notes_v2.md", "a/report.pdf"]
    assert [i["rel_path"] for i in second["items"]] == ["b/100%.md", "b/broken.docx"]

    recent = _get(api, order="recent", limit=1)
    assert [i["rel_path"] for i in recent["items"]] == ["b/broken.docx"]  # newest indexed_at


def test_a_run_filter_lists_what_that_run_touched(api: ApiHarness) -> None:
    _seed(api)

    body = _get(api, run_id="r1")

    assert [item["rel_path"] for item in body["items"]] == ["a/report.pdf"]


def test_bad_input_is_refused(api: ApiHarness) -> None:
    _seed(api)

    for params in ({"status": "bogus"}, {"order": "random"}, {"limit": 0}, {"offset": -1}):
        response = api.client.get("/v1/jobs/job-a/documents", headers=AUTH, params=params)
        assert response.status_code == 422, params
    assert api.client.get("/v1/jobs/nope/documents", headers=AUTH).status_code == 404
    assert api.client.get("/v1/jobs/job-a/documents").status_code == 401


def test_the_job_listing_carries_the_totals(api: ApiHarness) -> None:
    _seed(api)

    listing = api.client.get("/v1/jobs", headers=AUTH).json()

    assert listing[0]["documents"] == {"total": 5, "chunks": 7}


def test_health_announces_what_this_build_can_do(api: ApiHarness) -> None:
    features = api.client.get("/health").json()["features"]

    assert "run_progress" in features
    assert "documents" in features
    assert "delete_runs" in features


def test_run_rows_carry_the_progress_fields(api: ApiHarness) -> None:
    api.env.write_doc("a.md", "# A\n\nAlpha body.")
    api.write_jobs_yaml(api.default_job())
    api.engine.startup(fire_startup_runs=False)

    started = api.client.post("/v1/jobs/job-a/run", headers=AUTH, json={"dry_run": True})
    api.wait_run(started.json()["run_id"])

    run = api.client.get(f"/v1/runs/{started.json()['run_id']}", headers=AUTH).json()["run"]
    assert run["dry_run"] is True
    assert (run["files_seen"], run["files_done"]) == (1, 1)
    assert run["phase"] is None  # finished
