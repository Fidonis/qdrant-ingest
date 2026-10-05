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


def test_upsert_without_deleting_adds_and_replaces_but_removes_nothing(
    engine: EngineHarness,
) -> None:
    paths = [
        engine.write_doc(f"doc{i}.md", f"# D{i}\n\nBody {i}.") for i in range(4)
    ]
    job = engine.local_job(mode="upsert")
    engine.runner.run_job(job, "manual_rest")
    assert len(engine.sources_in_qdrant()) == 4

    paths[0].unlink()  # vanished from the scan
    paths[1].write_text("# D1\n\nBody rewritten.", encoding="utf-8")  # changed
    engine.write_doc("doc4.md", "# D4\n\nBody 4.")  # new
    run = engine.runner.run_job(job, "manual_rest", delete_vanished=False)

    assert run.status == "success"
    assert run.docs_deleted == 0
    assert run.docs_indexed == 2  # the changed and the new document
    sources = engine.sources_in_qdrant()
    assert "local://docs/doc0.md" in sources  # vanished, but kept
    assert "local://docs/doc4.md" in sources
    assert "local://docs/doc0.md" in engine.state.list_sources("job-a")
    stored = {p["source"]: p["text"] for p in engine.payloads()}
    assert "rewritten" in stored["local://docs/doc1.md"]


def test_upsert_without_deleting_does_not_trip_the_deletion_guards(
    engine: EngineHarness,
) -> None:
    # Files that were removed after they were embedded leave an empty scan behind,
    # which the empty-source guard would otherwise turn into an aborted run.
    paths = [engine.write_doc(f"doc{i}.md", f"# D{i}\n\nBody {i}.") for i in range(3)]
    job = engine.local_job(mode="upsert")
    engine.runner.run_job(job, "manual_rest")
    for path in paths:
        path.unlink()

    guarded = engine.runner.run_job(job, "manual_rest")
    assert guarded.status == "aborted_guard"

    run = engine.runner.run_job(job, "manual_rest", delete_vanished=False)

    assert run.status == "success"
    assert len(engine.sources_in_qdrant()) == 3


def test_a_reimported_file_under_the_same_path_updates_after_its_original_is_gone(
    engine: EngineHarness,
) -> None:
    path = engine.write_doc("kb/a.md", "# A\n\nAlpha original.")
    job = engine.local_job(mode="upsert")
    engine.runner.run_job(job, "manual_rest", delete_vanished=False)
    path.unlink()  # the staging area is cleaned up after a run

    engine.write_doc("kb/a.md", "# A\n\nAlpha updated.")
    run = engine.runner.run_job(job, "manual_rest", delete_vanished=False)

    assert run.status == "success"
    assert run.docs_indexed == 1
    stored = [p["text"] for p in engine.payloads() if p["source"] == "local://docs/kb/a.md"]
    assert len(stored) == 1 and "updated" in stored[0]


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
