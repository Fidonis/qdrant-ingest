"""The `_collection_meta` contract.

The point id derivation and payload shape must stay bit-identical to what the
consuming MCP server derives for query-time vectorisation — this is the one
constant shared between producer and consumer. One meta point per collection
also means exactly one embedding model per collection.
"""

import uuid

from qdrant_client.models import PointStruct

# Stable namespace so meta point ids are deterministic per collection name.
META_NAMESPACE = uuid.UUID("9e3a5c2f-8b7d-4f1e-a6b3-2d8c9e4f1a02")
# Single dummy vector — meta points are never queried by similarity.
META_VECTOR: list[float] = [0.0]


def meta_point_id(collection_name: str) -> str:
    """Deterministic UUID5 derived from the data collection name."""
    return str(uuid.uuid5(META_NAMESPACE, collection_name))


def make_meta_point(
    collection_name: str, embedding_model: str, vector_dimension: int
) -> PointStruct:
    return PointStruct(
        id=meta_point_id(collection_name),
        vector=META_VECTOR,
        payload={
            "collection": collection_name,
            "embedding_model": embedding_model,
            "vector_dimension": vector_dimension,
        },
    )
