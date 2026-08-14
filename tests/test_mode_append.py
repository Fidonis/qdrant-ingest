"""Mode `append`: add-only semantics and the state-loss probe."""

from conftest import EngineHarness


def test_append_only_embeds_new_documents(engine: EngineHarness) -> None:
    engine.write_doc("a.md", "# A\n\nAlpha.")
    job = engine.local_job(mode="append")
    first = engine.runner.run_job(job, "manual_rest")
    assert first.status == "success"
    assert first.docs_indexed == 1
    embedded_before = len(engine.embeddings.texts_embedded)

    engine.write_doc("b.md", "# B\n\nBravo.")
    second = engine.runner.run_job(job, "manual_rest")

    assert second.status == "success"
    assert second.docs_indexed == 1
    assert second.docs_unchanged == 1
    new_texts = engine.embeddings.texts_embedded[embedded_before:]
    assert all("Bravo" in text for text in new_texts)  # only the new document


def test_append_never_updates_but_reports_drift(engine: EngineHarness) -> None:
    path = engine.write_doc("a.md", "# A\n\nOriginal body.")
    job = engine.local_job(mode="append")
    engine.runner.run_job(job, "manual_rest")
    embedded_before = len(engine.embeddings.texts_embedded)

    path.write_text("# A\n\nCompletely rewritten body.", encoding="utf-8")
    run = engine.runner.run_job(job, "manual_rest")

    assert run.status == "success"
    assert run.docs_skipped_changed == 1  # drift is visible, not silent
    assert run.docs_indexed == 0
    assert len(engine.embeddings.texts_embedded) == embedded_before
    texts = [p["text"] for p in engine.payloads()]
    assert any("Original" in text for text in texts)  # old content retained


def test_append_never_deletes(engine: EngineHarness) -> None:
    path = engine.write_doc("a.md", "# A\n\nAlpha.")
    engine.write_doc("b.md", "# B\n\nBravo.")
    job = engine.local_job(mode="append")
    engine.runner.run_job(job, "manual_rest")

    path.unlink()
    run = engine.runner.run_job(job, "manual_rest")

    assert run.status == "success"
    assert run.docs_deleted == 0
    assert engine.sources_in_qdrant() == {"local://docs/a.md", "local://docs/b.md"}


def test_append_probe_auto_recovers_from_state_loss(engine: EngineHarness) -> None:
    engine.write_doc("a.md", "# A\n\nAlpha.")
    engine.write_doc("b.md", "# B\n\nBravo.")
    job = engine.local_job(mode="append")
    engine.runner.run_job(job, "manual_rest")
    embedded_before = len(engine.embeddings.texts_embedded)
    points_before = engine.qdrant.point_count("col-a")

    # The state volume is lost, the collection is not.
    engine.state.delete_documents_for_job("job-a")

    run = engine.runner.run_job(job, "manual_rest")

    assert run.status == "success"
    assert run.docs_indexed == 0
    # auto detected the loss and rebuilt the known set from the facet:
    # nothing was re-embedded, nothing was duplicated.
    assert len(engine.embeddings.texts_embedded) == embedded_before
    assert engine.qdrant.point_count("col-a") == points_before
    events = " ".join(e.message for e in engine.state.list_events(run.run_id))
    assert "facet" in events


def test_append_probe_state_re_embeds_after_state_loss(engine: EngineHarness) -> None:
    # Negative control for the auto probe: probing only the state would embed
    # the corpus a second time (deterministic ids keep it from duplicating,
    # but the embedding cost is real).
    engine.write_doc("a.md", "# A\n\nAlpha.")
    job = engine.local_job(mode="append", append_probe="state")
    engine.runner.run_job(job, "manual_rest")
    embedded_before = len(engine.embeddings.texts_embedded)

    engine.state.delete_documents_for_job("job-a")
    run = engine.runner.run_job(job, "manual_rest")

    assert run.status == "success"
    assert run.docs_indexed == 1
    assert len(engine.embeddings.texts_embedded) > embedded_before
