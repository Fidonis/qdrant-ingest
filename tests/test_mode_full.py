"""Mode `full`: generation sweep, crash safety, collection scope."""

import uuid

from conftest import EngineHarness


def test_full_run_indexes_and_writes_the_contract_payload(engine: EngineHarness) -> None:
    engine.write_doc("a.md", "# Doc A\n\nAlpha body text.")
    engine.write_doc("sub/b.md", "# Doc B\n\nBravo body text.")
    job = engine.local_job(mode="full")

    run = engine.runner.run_job(job, "manual_rest")

    assert run.status == "success"
    assert run.docs_indexed == 2
    assert run.files_seen == 2
    payloads = engine.payloads()
    assert payloads
    sample = next(p for p in payloads if p["source"] == "local://docs/a.md")
    assert sample["ingest_job"] == "job-a"
    assert sample["ingest_run"] == run.run_id
    assert sample["acl_tags"] == ["team:qa"]
    assert sample["origin"] == "test"  # extra_payload merged last
    assert sample["title"] == "Doc A"
    assert sample["file_name"] == "a.md"
    assert sample["chunk_index"] == 0
    assert sample["embedding_model"] == engine.settings.embedding_model
    assert engine.sources_in_qdrant() == {"local://docs/a.md", "local://docs/sub/b.md"}

    meta = engine.writer.read_meta("col-a")
    assert meta is not None
    assert meta["embedding_model"] == engine.settings.embedding_model
    assert meta["vector_dimension"] == engine.embeddings.dimension


def test_full_sweep_removes_deleted_documents(engine: EngineHarness) -> None:
    path_a = engine.write_doc("a.md", "# A\n\nAlpha.")
    engine.write_doc("b.md", "# B\n\nBravo.")
    job = engine.local_job(mode="full")
    engine.runner.run_job(job, "manual_rest")
    assert len(engine.sources_in_qdrant()) == 2

    path_a.unlink()
    run = engine.runner.run_job(job, "manual_rest")

    assert run.status == "success"
    assert engine.sources_in_qdrant() == {"local://docs/b.md"}
    assert engine.state.list_sources("job-a") == {"local://docs/b.md"}
    # The surviving document carries the new generation.
    assert all(p["ingest_run"] == run.run_id for p in engine.payloads())


def test_failed_full_run_never_sweeps(engine: EngineHarness) -> None:
    engine.write_doc("a.md", "# A\n\nAlpha.")
    engine.write_doc("b.md", "# B\n\nBravo.")
    job = engine.local_job(mode="full")
    first = engine.runner.run_job(job, "manual_rest")
    assert first.status == "success"

    # The endpoint dies mid-run: probe succeeds, the first embed batch fails.
    engine.embeddings.fail_after_texts = len(engine.embeddings.texts_embedded)
    run = engine.runner.run_job(job, "manual_rest")

    assert run.status == "failed"
    # No sweep ran: both documents' points survive under the old generation.
    assert engine.sources_in_qdrant() == {"local://docs/a.md", "local://docs/b.md"}
    assert all(p["ingest_run"] == first.run_id for p in engine.payloads())


def test_full_scope_collection_requires_force_with_siblings(
    engine: EngineHarness,
) -> None:
    engine.write_doc("a.md", "# A\n\nAlpha.")
    job = engine.local_job(mode="full", full_scope="collection")

    refused = engine.runner.run_job(
        job, "manual_rest", sibling_job_ids=["job-b"]
    )
    assert refused.status == "failed"
    assert refused.error is not None and "job-b" in refused.error

    forced = engine.runner.run_job(
        job, "manual_rest", sibling_job_ids=["job-b"], force=True
    )
    assert forced.status == "success"


def test_full_scope_collection_switches_the_embedding_model(
    engine: EngineHarness,
) -> None:
    engine.write_doc("a.md", "# A\n\nAlpha.")
    job = engine.local_job(mode="full")
    engine.runner.run_job(job, "manual_rest")
    assert engine.qdrant.collections["col-a"]["dim"] == 8

    # Same collection, new model with a different dimension: only the
    # collection-scope path may rebuild it.
    engine.embeddings.dimension = 4
    job_switched = engine.local_job(
        mode="full", full_scope="collection", embedding={"model": "model-b"}
    )
    run = engine.runner.run_job(job_switched, "manual_rest")

    assert run.status == "success"
    assert engine.qdrant.collections["col-a"]["dim"] == 4
    meta = engine.writer.read_meta("col-a")
    assert meta is not None
    assert meta["embedding_model"] == "model-b"
    assert meta["vector_dimension"] == 4


def test_full_dry_run_writes_nothing(engine: EngineHarness) -> None:
    engine.write_doc("a.md", "# A\n\nAlpha.")
    job = engine.local_job(mode="full")

    run = engine.runner.run_job(job, "manual_rest", dry_run=True)

    assert run.status == "success"
    assert run.docs_indexed == 1  # what WOULD be indexed
    assert engine.qdrant.point_count("col-a") == 0
    assert "col-a" not in engine.qdrant.collections
    assert engine.state.list_sources("job-a") == set()
    assert engine.embeddings.texts_embedded == []


def test_run_ids_are_uuids(engine: EngineHarness) -> None:
    engine.write_doc("a.md", "# A\n\nAlpha.")
    run = engine.runner.run_job(engine.local_job(mode="full"), "cron")
    assert uuid.UUID(run.run_id)
    assert run.trigger == "cron"
    assert run.finished_at is not None
