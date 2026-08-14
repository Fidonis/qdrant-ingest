"""The `_collection_meta` contract, checked against literal constants.

The UUID below is typed out on purpose — importing it from the code under
test would make this a tautology instead of a contract check against the
consuming MCP server's derivation.
"""

import uuid

from store import META_NAMESPACE, META_VECTOR, make_meta_point, meta_point_id

CONTRACT_NAMESPACE = uuid.UUID("9e3a5c2f-8b7d-4f1e-a6b3-2d8c9e4f1a02")


def test_namespace_is_the_contract_value() -> None:
    assert META_NAMESPACE == CONTRACT_NAMESPACE


def test_meta_vector_is_single_zero() -> None:
    assert META_VECTOR == [0.0]


def test_point_id_derivation_matches_consumer() -> None:
    for name in ("corporate-knowledge", "ops-runbooks", "fx-full"):
        assert meta_point_id(name) == str(uuid.uuid5(CONTRACT_NAMESPACE, name))


def test_meta_point_payload_shape() -> None:
    point = make_meta_point("corporate-knowledge", "nomic-embed-text", 768)
    assert point.id == str(uuid.uuid5(CONTRACT_NAMESPACE, "corporate-knowledge"))
    assert point.vector == [0.0]
    assert point.payload == {
        "collection": "corporate-knowledge",
        "embedding_model": "nomic-embed-text",
        "vector_dimension": 768,
    }
