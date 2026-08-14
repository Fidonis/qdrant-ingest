"""Chunk point-id derivation."""

import uuid

from store import point_id


def test_deterministic() -> None:
    assert point_id("job-a", "s3://x/a.pdf", 0) == point_id("job-a", "s3://x/a.pdf", 0)


def test_distinct_per_component() -> None:
    base = point_id("job-a", "s3://x/a.pdf", 0)
    assert point_id("job-b", "s3://x/a.pdf", 0) != base
    assert point_id("job-a", "s3://x/b.pdf", 0) != base
    assert point_id("job-a", "s3://x/a.pdf", 1) != base


def test_is_uuid5() -> None:
    parsed = uuid.UUID(point_id("job-a", "s3://x/a.pdf", 0))
    assert parsed.version == 5


def test_not_the_predecessor_namespace() -> None:
    # Same input string under the historic seed must NOT collide: the new
    # namespace is intentionally distinct, and no delete path depends on ids.
    old_namespace = uuid.uuid5(uuid.NAMESPACE_URL, "qdrant-nextcloud-ingest")
    old_id = str(uuid.uuid5(old_namespace, "job-a|s3://x/a.pdf#0"))
    assert point_id("job-a", "s3://x/a.pdf", 0) != old_id
