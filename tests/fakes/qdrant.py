"""Stateful in-memory Qdrant stand-in.

Implements exactly the client subset the writer uses, with *real*
payload-filter semantics — the generation sweep, the guards, and the
append probe are meaningless against a mock that does not actually filter.
"""

from collections import Counter
from types import SimpleNamespace
from typing import Any

from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    VectorParams,
)


def _condition_matches(condition: Any, payload: dict[str, Any]) -> bool:
    if not isinstance(condition, FieldCondition):
        raise NotImplementedError(f"unsupported condition {type(condition).__name__}")
    value = payload.get(condition.key)
    match = condition.match
    if isinstance(match, MatchValue):
        if isinstance(value, list):
            return match.value in value
        return value == match.value
    if isinstance(match, MatchAny):
        candidates = set(match.any)
        if isinstance(value, list):
            return bool(candidates.intersection(value))
        return value in candidates
    raise NotImplementedError(f"unsupported match {type(match).__name__}")


def _as_list(value: Any) -> list[Any]:
    # The real server accepts a single condition where a list is allowed.
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def filter_matches(flt: Filter | None, payload: dict[str, Any]) -> bool:
    if flt is None:
        return True
    for condition in _as_list(flt.must):
        if not _condition_matches(condition, payload):
            return False
    return all(
        not _condition_matches(condition, payload) for condition in _as_list(flt.must_not)
    )


class FakeQdrant:
    def __init__(self) -> None:
        # name -> {"dim": int, "points": {id: {"vector": [...], "payload": {...}}},
        #          "indexes": set[str]}
        self.collections: dict[str, dict[str, Any]] = {}
        self.upsert_calls = 0
        self.raise_on_get_collections: Exception | None = None

    # ── collection lifecycle ─────────────────────────────────────────────────

    def get_collections(self) -> SimpleNamespace:
        if self.raise_on_get_collections is not None:
            exc = self.raise_on_get_collections
            self.raise_on_get_collections = None
            raise exc
        return SimpleNamespace(
            collections=[SimpleNamespace(name=name) for name in self.collections]
        )

    def create_collection(self, collection_name: str, vectors_config: VectorParams) -> None:
        self.collections[collection_name] = {
            "dim": int(vectors_config.size),
            "points": {},
            "indexes": set(),
        }

    def delete_collection(self, collection_name: str) -> None:
        self.collections.pop(collection_name, None)

    def get_collection(self, collection_name: str) -> SimpleNamespace:
        record = self.collections[collection_name]
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors=VectorParams(size=record["dim"], distance=Distance.COSINE)
                )
            ),
            payload_schema={field: "keyword" for field in record["indexes"]},
        )

    def create_payload_index(
        self,
        collection_name: str,
        field_name: str,
        field_schema: Any = None,
        wait: bool = True,
    ) -> None:
        self.collections[collection_name]["indexes"].add(field_name)

    # ── points ───────────────────────────────────────────────────────────────

    def upsert(self, collection_name: str, points: list[Any], wait: bool = True) -> None:
        self.upsert_calls += 1
        store = self.collections[collection_name]["points"]
        for point in points:
            store[str(point.id)] = {
                "vector": list(point.vector),
                "payload": dict(point.payload or {}),
            }

    def delete(self, collection_name: str, points_selector: Any, wait: bool = True) -> None:
        flt = points_selector.filter
        store = self.collections[collection_name]["points"]
        doomed = [
            point_id
            for point_id, record in store.items()
            if filter_matches(flt, record["payload"])
        ]
        for point_id in doomed:
            del store[point_id]

    def retrieve(
        self, collection_name: str, ids: list[Any], with_payload: bool = True
    ) -> list[SimpleNamespace]:
        store = self.collections.get(collection_name, {"points": {}})["points"]
        result = []
        for point_id in ids:
            record = store.get(str(point_id))
            if record is not None:
                result.append(
                    SimpleNamespace(
                        id=str(point_id),
                        payload=dict(record["payload"]),
                        vector=list(record["vector"]),
                    )
                )
        return result

    def count(
        self, collection_name: str, count_filter: Filter | None = None, exact: bool = True
    ) -> SimpleNamespace:
        store = self.collections.get(collection_name, {"points": {}})["points"]
        matched = sum(
            1 for record in store.values() if filter_matches(count_filter, record["payload"])
        )
        return SimpleNamespace(count=matched)

    def facet(
        self,
        collection_name: str,
        key: str,
        facet_filter: Filter | None = None,
        limit: int = 10_000,
    ) -> SimpleNamespace:
        store = self.collections.get(collection_name, {"points": {}})["points"]
        counter: Counter[str] = Counter()
        for record in store.values():
            if not filter_matches(facet_filter, record["payload"]):
                continue
            value = record["payload"].get(key)
            if isinstance(value, list):
                counter.update(str(item) for item in value)
            elif value is not None:
                counter[str(value)] += 1
        hits = [
            SimpleNamespace(value=value, count=count)
            for value, count in counter.most_common(limit)
        ]
        return SimpleNamespace(hits=hits)

    def scroll(
        self,
        collection_name: str,
        scroll_filter: Filter | None = None,
        limit: int = 100,
        offset: Any = None,
        with_payload: bool = True,
    ) -> tuple[list[SimpleNamespace], Any]:
        store = self.collections.get(collection_name, {"points": {}})["points"]
        matched = [
            SimpleNamespace(
                id=point_id, payload=dict(record["payload"]), vector=list(record["vector"])
            )
            for point_id, record in sorted(store.items())
            if filter_matches(scroll_filter, record["payload"])
        ]
        return matched[:limit], None

    # ── test conveniences ────────────────────────────────────────────────────

    def payloads(self, collection_name: str) -> list[dict[str, Any]]:
        store = self.collections.get(collection_name, {"points": {}})["points"]
        return [dict(record["payload"]) for record in store.values()]

    def point_count(self, collection_name: str) -> int:
        return len(self.collections.get(collection_name, {"points": {}})["points"])
