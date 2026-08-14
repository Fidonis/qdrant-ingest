"""The Qdrant writer.

Writes points directly over the Qdrant API with the service's api-key: the
`_collection_meta` record must be written on every ingest, and system
collections are rejected by the MCP layer's mutation tools by design — so
ingestion cannot go through MCP.

All delete operations are payload-filter based (never id lookups); see
store/ids.py for why that is load-bearing.
"""

import time
from collections.abc import Iterable, Sequence

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PointStruct,
    VectorParams,
)

from store.indexes import ensure_payload_indexes
from store.meta import make_meta_point, meta_point_id


class DimensionMismatchError(RuntimeError):
    """The collection exists with a different vector dimension."""


class ModelMismatchError(RuntimeError):
    """The collection's meta record names a different embedding model."""


def _job_filter(job_id: str) -> FieldCondition:
    return FieldCondition(key="ingest_job", match=MatchValue(value=job_id))


class QdrantWriter:
    """Collection lifecycle plus scoped, generation-tagged writes."""

    def __init__(self, client: QdrantClient, meta_collection: str) -> None:
        self._client = client
        self._meta_collection = meta_collection

    @property
    def client(self) -> QdrantClient:
        return self._client

    # ── readiness ────────────────────────────────────────────────────────────

    def wait_ready(self, retries: int = 30, delay_seconds: float = 3.0) -> bool:
        for _attempt in range(retries):
            if self.ping():
                return True
            time.sleep(delay_seconds)
        return False

    def ping(self) -> bool:
        try:
            self._client.get_collections()
        except Exception:  # noqa: BLE001 - any transport failure means "not ready"
            return False
        return True

    # ── collection lifecycle ─────────────────────────────────────────────────

    def collection_names(self) -> set[str]:
        return {c.name for c in self._client.get_collections().collections}

    def collection_dimension(self, collection: str) -> int:
        info = self._client.get_collection(collection)
        vectors = info.config.params.vectors
        assert isinstance(vectors, VectorParams)  # unnamed default vector only
        return int(vectors.size)

    def _ensure_meta_collection(self) -> None:
        if self._meta_collection not in self.collection_names():
            self._client.create_collection(
                collection_name=self._meta_collection,
                vectors_config=VectorParams(size=1, distance=Distance.COSINE),
            )

    def ensure_collection(
        self, collection: str, vector_dim: int, embedding_model: str
    ) -> None:
        """Create or verify the target collection, meta record, and indexes."""
        self._ensure_meta_collection()

        if collection not in self.collection_names():
            self._client.create_collection(
                collection_name=collection,
                vectors_config=VectorParams(size=vector_dim, distance=Distance.COSINE),
            )
        else:
            existing_dim = self.collection_dimension(collection)
            if existing_dim != vector_dim:
                raise DimensionMismatchError(
                    f"collection '{collection}' has dimension {existing_dim}, but "
                    f"model '{embedding_model}' produces {vector_dim}; a full run "
                    "with full_scope: collection is the supported way to rebuild "
                    "the collection with a new dimension"
                )
            meta = self.read_meta(collection)
            if meta is not None:
                recorded_model = meta.get("embedding_model")
                if recorded_model and recorded_model != embedding_model:
                    raise ModelMismatchError(
                        f"collection '{collection}' is recorded with embedding model "
                        f"'{recorded_model}', refusing to write with '{embedding_model}'"
                    )

        ensure_payload_indexes(self._client, collection)

    def recreate_collection(self, collection: str, vector_dim: int) -> None:
        """full_scope=collection: the one supported way to change dimensions."""
        if collection in self.collection_names():
            self._client.delete_collection(collection_name=collection)
        self._client.create_collection(
            collection_name=collection,
            vectors_config=VectorParams(size=vector_dim, distance=Distance.COSINE),
        )
        ensure_payload_indexes(self._client, collection)

    # ── meta record ──────────────────────────────────────────────────────────

    def upsert_meta(self, collection: str, embedding_model: str, vector_dim: int) -> None:
        self._ensure_meta_collection()
        self._client.upsert(
            collection_name=self._meta_collection,
            points=[make_meta_point(collection, embedding_model, vector_dim)],
            wait=True,
        )

    def read_meta(self, collection: str) -> dict[str, object] | None:
        records = self._client.retrieve(
            collection_name=self._meta_collection,
            ids=[meta_point_id(collection)],
            with_payload=True,
        )
        if not records:
            return None
        payload = records[0].payload
        return dict(payload) if payload else None

    # ── writes and deletes ───────────────────────────────────────────────────

    def upsert_points(self, collection: str, points: Sequence[PointStruct]) -> None:
        if points:
            self._client.upsert(collection_name=collection, points=list(points), wait=True)

    def delete_by_source(self, collection: str, job_id: str, source: str) -> None:
        self._client.delete(
            collection_name=collection,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[
                        _job_filter(job_id),
                        FieldCondition(key="source", match=MatchValue(value=source)),
                    ]
                )
            ),
            wait=True,
        )

    def delete_job_points(self, collection: str, job_id: str) -> None:
        self._client.delete(
            collection_name=collection,
            points_selector=FilterSelector(filter=Filter(must=[_job_filter(job_id)])),
            wait=True,
        )

    def sweep_stale(self, collection: str, run_id: str, job_id: str | None) -> None:
        """The generation sweep: everything this run did not touch.

        scope=job:        ingest_job == job_id AND ingest_run != run_id
        scope=collection: ingest_run != run_id
        """
        stale = FieldCondition(key="ingest_run", match=MatchValue(value=run_id))
        if job_id is not None:
            sweep_filter = Filter(must=_job_filter(job_id), must_not=stale)
        else:
            sweep_filter = Filter(must_not=stale)
        self._client.delete(
            collection_name=collection,
            points_selector=FilterSelector(filter=sweep_filter),
            wait=True,
        )

    # ── reads for probes and reporting ───────────────────────────────────────

    def payload_index_fields(self, collection: str) -> set[str]:
        info = self._client.get_collection(collection)
        schema = getattr(info, "payload_schema", None) or {}
        return set(schema.keys())

    def count_points(self, collection: str, job_id: str | None = None) -> int:
        count_filter = Filter(must=[_job_filter(job_id)]) if job_id else None
        return int(
            self._client.count(
                collection_name=collection, count_filter=count_filter, exact=True
            ).count
        )

    def facet_sources(self, collection: str, job_id: str, limit: int = 10_000) -> set[str]:
        """Distinct payload `source` values of one job, from Qdrant itself.

        Used by append_probe when the state volume was lost — one request
        against the keyword index instead of O(files) lookups.
        """
        result = self._client.facet(
            collection_name=collection,
            key="source",
            facet_filter=Filter(must=[_job_filter(job_id)]),
            limit=limit,
        )
        return {str(hit.value) for hit in result.hits}

    def job_point_counts(self, collections: Iterable[str], job_id: str) -> dict[str, int]:
        return {
            collection: self.count_points(collection, job_id)
            for collection in collections
        }
