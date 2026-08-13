"""Payload indexes required by the ingestion and RBAC contracts.

Index creation lives inside ensure_collection and runs on every call because
delete_collection (full_scope: collection) destroys indexes with the
collection — and create_payload_index is idempotent.
"""

from qdrant_client import QdrantClient
from qdrant_client.models import PayloadSchemaType

# source: list_documents facets on it. ingest_job / ingest_run: job scoping
# and the generation sweep. acl_tags: doc_policy MatchAny filtering.
PAYLOAD_INDEX_FIELDS: tuple[str, ...] = ("source", "ingest_job", "ingest_run", "acl_tags")


def ensure_payload_indexes(client: QdrantClient, collection: str) -> None:
    for field in PAYLOAD_INDEX_FIELDS:
        client.create_payload_index(
            collection_name=collection,
            field_name=field,
            field_schema=PayloadSchemaType.KEYWORD,
            wait=True,
        )
