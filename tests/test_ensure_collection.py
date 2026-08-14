"""Collection lifecycle: creation, dimension and model checks, indexes."""

import pytest
from qdrant_client import QdrantClient

from store import DimensionMismatchError, ModelMismatchError, QdrantWriter
from store.indexes import PAYLOAD_INDEX_FIELDS

from fakes.qdrant import FakeQdrant


def _writer(fake: FakeQdrant) -> QdrantWriter:
    # The fake implements the client subset the writer uses.
    return QdrantWriter(client=fake, meta_collection="_collection_meta")  # type: ignore[arg-type]


def test_creates_collection_meta_and_indexes() -> None:
    fake = FakeQdrant()
    writer = _writer(fake)
    writer.ensure_collection("col-a", 768, "model-x")
    assert "col-a" in fake.collections
    assert "_collection_meta" in fake.collections
    assert fake.collections["col-a"]["indexes"] == set(PAYLOAD_INDEX_FIELDS)
    assert fake.collections["col-a"]["dim"] == 768


def test_ensure_is_idempotent() -> None:
    fake = FakeQdrant()
    writer = _writer(fake)
    writer.ensure_collection("col-a", 768, "model-x")
    writer.upsert_meta("col-a", "model-x", 768)
    writer.ensure_collection("col-a", 768, "model-x")
    assert fake.collections["col-a"]["dim"] == 768


def test_dimension_mismatch_names_both_and_the_escape() -> None:
    fake = FakeQdrant()
    writer = _writer(fake)
    writer.ensure_collection("col-a", 768, "model-x")
    with pytest.raises(DimensionMismatchError) as excinfo:
        writer.ensure_collection("col-a", 384, "model-y")
    message = str(excinfo.value)
    assert "768" in message
    assert "384" in message
    assert "full_scope: collection" in message


def test_model_mismatch_is_refused() -> None:
    fake = FakeQdrant()
    writer = _writer(fake)
    writer.ensure_collection("col-a", 768, "model-x")
    writer.upsert_meta("col-a", "model-x", 768)
    with pytest.raises(ModelMismatchError, match="model-x"):
        writer.ensure_collection("col-a", 768, "model-y")


def test_meta_roundtrip_single_point_per_collection() -> None:
    fake = FakeQdrant()
    writer = _writer(fake)
    writer.upsert_meta("col-a", "model-x", 768)
    writer.upsert_meta("col-a", "model-x", 768)  # idempotent overwrite
    assert fake.point_count("_collection_meta") == 1
    meta = writer.read_meta("col-a")
    assert meta == {
        "collection": "col-a",
        "embedding_model": "model-x",
        "vector_dimension": 768,
    }
    assert writer.read_meta("unknown-col") is None


def test_recreate_collection_resets_points_and_indexes() -> None:
    fake = FakeQdrant()
    writer = _writer(fake)
    writer.ensure_collection("col-a", 768, "model-x")
    fake.collections["col-a"]["points"]["p1"] = {"vector": [0.0], "payload": {}}
    writer.recreate_collection("col-a", 384)
    assert fake.point_count("col-a") == 0
    assert fake.collections["col-a"]["dim"] == 384
    assert fake.collections["col-a"]["indexes"] == set(PAYLOAD_INDEX_FIELDS)


def test_ping_reports_transport_failures() -> None:
    fake = FakeQdrant()
    writer = _writer(fake)
    assert writer.ping() is True
    fake.raise_on_get_collections = ConnectionError("down")
    assert writer.ping() is False


def test_writer_accepts_real_client_type() -> None:
    # Interface sanity: the writer is annotated against the real client class.
    client = QdrantClient(location=":memory:")
    writer = QdrantWriter(client=client, meta_collection="_collection_meta")
    assert writer.ping()
