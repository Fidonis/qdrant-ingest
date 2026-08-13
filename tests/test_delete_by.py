"""Filter-based deletes: per-source, per-job, and the generation sweep."""

from qdrant_client.models import PointStruct

from store import QdrantWriter, point_id

from fakes.qdrant import FakeQdrant


def _seed(writer: QdrantWriter, job_id: str, source: str, run_id: str, chunks: int) -> None:
    points = [
        PointStruct(
            id=point_id(job_id, source, i),
            vector=[0.1, 0.2],
            payload={
                "text": f"chunk {i} of {source}",
                "source": source,
                "ingest_job": job_id,
                "ingest_run": run_id,
                "chunk_index": i,
            },
        )
        for i in range(chunks)
    ]
    writer.upsert_points("col", points)


def _setup() -> tuple[FakeQdrant, QdrantWriter]:
    fake = FakeQdrant()
    writer = QdrantWriter(client=fake, meta_collection="_collection_meta")  # type: ignore[arg-type]
    writer.ensure_collection("col", 2, "model-x")
    return fake, writer


def test_delete_by_source_is_job_scoped() -> None:
    fake, writer = _setup()
    _seed(writer, "job-a", "local://a/doc.md", "run-1", 3)
    _seed(writer, "job-b", "local://b/doc.md", "run-1", 2)
    writer.delete_by_source("col", "job-a", "local://a/doc.md")
    remaining = fake.payloads("col")
    assert len(remaining) == 2
    assert all(p["ingest_job"] == "job-b" for p in remaining)


def test_sweep_job_scope_removes_only_stale_points_of_that_job() -> None:
    fake, writer = _setup()
    _seed(writer, "job-a", "local://a/old.md", "run-old", 2)
    _seed(writer, "job-a", "local://a/new.md", "run-new", 2)
    _seed(writer, "job-b", "local://b/other.md", "run-elsewhere", 2)

    writer.sweep_stale("col", run_id="run-new", job_id="job-a")

    remaining = fake.payloads("col")
    assert len(remaining) == 4
    job_a_sources = {p["source"] for p in remaining if p["ingest_job"] == "job-a"}
    assert job_a_sources == {"local://a/new.md"}  # stale generation swept
    job_b_sources = {p["source"] for p in remaining if p["ingest_job"] == "job-b"}
    assert job_b_sources == {"local://b/other.md"}  # sibling job untouched


def test_sweep_collection_scope_ignores_job_boundaries() -> None:
    fake, writer = _setup()
    _seed(writer, "job-a", "local://a/old.md", "run-old", 1)
    _seed(writer, "job-b", "local://b/old.md", "run-old", 1)
    _seed(writer, "job-a", "local://a/new.md", "run-new", 1)

    writer.sweep_stale("col", run_id="run-new", job_id=None)

    remaining = fake.payloads("col")
    assert len(remaining) == 1
    assert remaining[0]["source"] == "local://a/new.md"


def test_shrinking_document_is_cleaned_by_delete_before_upsert() -> None:
    fake, writer = _setup()
    _seed(writer, "job-a", "local://a/doc.md", "run-1", 5)
    # Re-ingest with fewer chunks: delete_by(source) precedes the upsert.
    writer.delete_by_source("col", "job-a", "local://a/doc.md")
    _seed(writer, "job-a", "local://a/doc.md", "run-2", 3)
    assert fake.point_count("col") == 3


def test_delete_job_points() -> None:
    fake, writer = _setup()
    _seed(writer, "job-a", "local://a/doc.md", "run-1", 2)
    _seed(writer, "job-b", "local://b/doc.md", "run-1", 2)
    writer.delete_job_points("col", "job-a")
    assert {p["ingest_job"] for p in fake.payloads("col")} == {"job-b"}


def test_count_points_with_job_filter() -> None:
    _, writer = _setup()
    _seed(writer, "job-a", "local://a/doc.md", "run-1", 3)
    _seed(writer, "job-b", "local://b/doc.md", "run-1", 2)
    assert writer.count_points("col") == 5
    assert writer.count_points("col", job_id="job-a") == 3


def test_facet_sources_per_job() -> None:
    _, writer = _setup()
    _seed(writer, "job-a", "local://a/one.md", "run-1", 2)
    _seed(writer, "job-a", "local://a/two.md", "run-1", 1)
    _seed(writer, "job-b", "local://b/other.md", "run-1", 1)
    assert writer.facet_sources("col", "job-a") == {
        "local://a/one.md",
        "local://a/two.md",
    }
