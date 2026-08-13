"""The four-stage change detection."""

import json
import os

from conftest import EngineHarness


def test_stage1_unchanged_stat_skips_without_io(engine: EngineHarness) -> None:
    engine.write_doc("a.md", "# A\n\nAlpha.")
    job = engine.local_job(mode="upsert")
    engine.runner.run_job(job, "manual_rest")
    embedded_before = len(engine.embeddings.texts_embedded)

    run = engine.runner.run_job(job, "manual_rest")

    assert run.status == "success"
    assert run.docs_unchanged == 1
    assert run.bytes_read == 0  # stage 1 never opened the file
    assert len(engine.embeddings.texts_embedded) == embedded_before


def test_stage2_touch_only_change_does_not_reembed(engine: EngineHarness) -> None:
    path = engine.write_doc("a.md", "# A\n\nAlpha.")
    job = engine.local_job(mode="upsert")
    engine.runner.run_job(job, "manual_rest")
    embedded_before = len(engine.embeddings.texts_embedded)
    row_before = engine.state.get_document("job-a", "local://docs/a.md")
    assert row_before is not None

    # Touch: new mtime, identical bytes — a cache-volume loss looks like this
    # for every file at once.
    os.utime(path, ns=(path.stat().st_atime_ns, row_before.mtime_ns + 5_000_000_000))
    run = engine.runner.run_job(job, "manual_rest")

    assert run.status == "success"
    assert run.docs_unchanged == 1
    assert run.bytes_read > 0  # the hash was computed
    assert len(engine.embeddings.texts_embedded) == embedded_before
    row_after = engine.state.get_document("job-a", "local://docs/a.md")
    assert row_after is not None
    assert row_after.mtime_ns == row_before.mtime_ns + 5_000_000_000  # stat advanced


def test_stage3_binary_change_with_identical_text(engine: EngineHarness) -> None:
    # A re-saved PDF: different bytes, same extracted text. The fake Tika
    # returns the same canned text regardless of the body.
    engine.tika.set_response(
        "doc.pdf",
        [
            {
                "X-TIKA:content": "x" * 200,
                "Content-Type": "application/pdf",
                "xmpTPg:NPages": "1",
            }
        ],
    )
    path = engine.docs_dir / "doc.pdf"
    path.write_bytes(b"%PDF-version-one")
    job = engine.local_job(mode="upsert")
    engine.runner.run_job(job, "manual_rest")
    embedded_before = len(engine.embeddings.texts_embedded)
    row_before = engine.state.get_document("job-a", "local://docs/doc.pdf")
    assert row_before is not None

    path.write_bytes(b"%PDF-version-two-different-bytes")
    run = engine.runner.run_job(job, "manual_rest")

    assert run.status == "success"
    assert run.docs_unchanged == 1
    assert len(engine.embeddings.texts_embedded) == embedded_before  # no re-embed
    row_after = engine.state.get_document("job-a", "local://docs/doc.pdf")
    assert row_after is not None
    assert row_after.content_sha != row_before.content_sha  # hash advanced


def test_stage4_parameter_change_forces_reembed(engine: EngineHarness) -> None:
    engine.write_doc("a.md", "# A\n\nAlpha body.")
    engine.runner.run_job(engine.local_job(mode="upsert"), "manual_rest")
    embedded_before = len(engine.embeddings.texts_embedded)

    # Same file, same stat — but the chunking geometry changed.
    job_rechunked = engine.local_job(
        mode="upsert", chunking={"words": 512, "overlap": 64}
    )
    run = engine.runner.run_job(job_rechunked, "manual_rest")

    assert run.status == "success"
    assert run.docs_indexed == 1
    assert run.docs_unchanged == 0
    assert len(engine.embeddings.texts_embedded) > embedded_before


def test_content_change_replaces_points(engine: EngineHarness) -> None:
    path = engine.write_doc("a.md", "# A\n\n" + " ".join(f"w{i}" for i in range(600)))
    job = engine.local_job(mode="upsert")
    engine.runner.run_job(job, "manual_rest")
    chunks_before = engine.qdrant.point_count("col-a")
    assert chunks_before > 1

    # Shrink the document sharply: stale chunk indexes must not survive.
    path.write_text("# A\n\nshort now", encoding="utf-8")
    run = engine.runner.run_job(job, "manual_rest")

    assert run.status == "success"
    assert engine.qdrant.point_count("col-a") == 1
    row = engine.state.get_document("job-a", "local://docs/a.md")
    assert row is not None
    assert row.chunk_count == 1
    assert row.last_run_id == run.run_id


def test_data_file_change_detection_end_to_end(engine: EngineHarness) -> None:
    path = engine.docs_dir / "config.json"
    path.write_text(json.dumps({"a": 1}), encoding="utf-8")
    job = engine.local_job(mode="upsert")
    engine.runner.run_job(job, "manual_rest")
    embedded_before = len(engine.embeddings.texts_embedded)

    path.write_text(json.dumps({"a": 2}), encoding="utf-8")
    run = engine.runner.run_job(job, "manual_rest")

    assert run.status == "success"
    assert run.docs_indexed == 1
    assert len(engine.embeddings.texts_embedded) > embedded_before
