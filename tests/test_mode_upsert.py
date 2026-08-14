"""Mode `upsert`: update-in-place plus vanished-source deletion."""

from conftest import EngineHarness


def test_upsert_reembeds_only_changed_documents(engine: EngineHarness) -> None:
    path = engine.write_doc("a.md", "# A\n\nAlpha original.")
    engine.write_doc("b.md", "# B\n\nBravo stays.")
    job = engine.local_job(mode="upsert")
    engine.runner.run_job(job, "manual_rest")
    embedded_before = len(engine.embeddings.texts_embedded)

    path.write_text("# A\n\nAlpha rewritten.", encoding="utf-8")
    run = engine.runner.run_job(job, "manual_rest")

    assert run.status == "success"
    assert run.docs_indexed == 1
    assert run.docs_unchanged == 1
    new_texts = engine.embeddings.texts_embedded[embedded_before:]
    assert new_texts and all("Alpha" in text for text in new_texts)
    stored = {p["source"]: p["text"] for p in engine.payloads()}
    assert "rewritten" in stored["local://docs/a.md"]


def test_upsert_deletes_vanished_sources(engine: EngineHarness) -> None:
    paths = [
        engine.write_doc(f"doc{i}.md", f"# D{i}\n\nBody {i}.") for i in range(4)
    ]
    job = engine.local_job(mode="upsert")
    engine.runner.run_job(job, "manual_rest")
    assert len(engine.sources_in_qdrant()) == 4

    paths[0].unlink()  # 1 of 4 vanished: at the 25% default ratio, allowed
    run = engine.runner.run_job(job, "manual_rest")

    assert run.status == "success"
    assert run.docs_deleted == 1
    assert "local://docs/doc0.md" not in engine.sources_in_qdrant()
    assert "local://docs/doc0.md" not in engine.state.list_sources("job-a")


def test_failed_upsert_run_skips_the_delete_phase(engine: EngineHarness) -> None:
    path_a = engine.write_doc("a.md", "# A\n\nAlpha.")
    engine.write_doc("b.md", "# B\n\nBravo.")
    job = engine.local_job(mode="upsert")
    engine.runner.run_job(job, "manual_rest")

    # One file vanishes AND the endpoint dies while re-embedding another.
    path_a.unlink()
    engine.write_doc("b.md", "# B\n\nBravo changed.")
    engine.embeddings.fail_after_texts = len(engine.embeddings.texts_embedded)
    run = engine.runner.run_job(job, "manual_rest")

    assert run.status == "failed"
    # The vanished document's points survive: no deletion without a clean scan.
    assert "local://docs/a.md" in engine.sources_in_qdrant()


def test_failed_extraction_is_not_treated_as_vanished(engine: EngineHarness) -> None:
    engine.write_doc("ok.md", "# OK\n\nFine.")
    broken = engine.docs_dir / "broken.pdf"
    broken.write_bytes(b"%PDF-broken")
    engine.tika.status_queue = [422]  # terminal: unsupported/encrypted
    job = engine.local_job(mode="upsert")

    run = engine.runner.run_job(job, "manual_rest")

    assert run.status == "success"
    assert run.docs_failed == 1
    assert run.docs_deleted == 0
    row = engine.state.get_document("job-a", "local://docs/broken.pdf")
    assert row is not None
    assert row.status == "failed_extract"


def test_upsert_dry_run_reports_without_writing(engine: EngineHarness) -> None:
    engine.write_doc("a.md", "# A\n\nAlpha.")
    job = engine.local_job(mode="upsert")

    run = engine.runner.run_job(job, "manual_rest", dry_run=True)

    assert run.status == "success"
    assert run.docs_indexed == 1
    assert engine.embeddings.texts_embedded == []
    assert engine.state.list_sources("job-a") == set()
