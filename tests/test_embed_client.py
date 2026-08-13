"""The httpx embeddings client."""

import json

import httpx
import pytest

from embed import EmbeddingClient, EmbeddingUnavailableError


class StubEndpoint:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.auth_headers: list[str] = []
        self.status_queue: list[int] = []
        self.reverse_order = False
        self.dimension = 4

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.auth_headers.append(request.headers.get("Authorization", ""))
        if self.status_queue:
            return httpx.Response(self.status_queue.pop(0), text="forced error")
        body = json.loads(request.content.decode("utf-8"))
        self.requests.append(body)
        texts = body["input"]
        data = [
            {
                "object": "embedding",
                "index": i,
                "embedding": [float(i)] * self.dimension,
            }
            for i in range(len(texts))
        ]
        if self.reverse_order:
            data = list(reversed(data))
        return httpx.Response(
            200, json={"object": "list", "model": body["model"], "data": data}
        )


def _client(stub: StubEndpoint, retries: int = 3) -> EmbeddingClient:
    return EmbeddingClient(
        "http://embed.test/v1",
        api_key="test-key",
        model="stub-model",
        retries=retries,
        transport=stub.transport(),
        sleep=lambda _delay: None,
    )


def test_out_of_order_response_is_sorted_by_index() -> None:
    stub = StubEndpoint()
    stub.reverse_order = True  # the provider returns data reversed
    vectors = _client(stub).embed_batch(["a", "b", "c"])
    assert vectors[0][0] == 0.0
    assert vectors[1][0] == 1.0
    assert vectors[2][0] == 2.0


def test_batches_are_split_by_batch_size() -> None:
    stub = StubEndpoint()
    vectors = _client(stub).embed_all([f"t{i}" for i in range(5)], batch_size=2)
    assert len(vectors) == 5
    assert [len(request["input"]) for request in stub.requests] == [2, 2, 1]


def test_retry_on_5xx_then_success() -> None:
    stub = StubEndpoint()
    stub.status_queue = [502]
    vectors = _client(stub).embed_batch(["a"])
    assert len(vectors) == 1


def test_persistent_failure_raises_unavailable() -> None:
    stub = StubEndpoint()
    stub.status_queue = [500, 500, 500]
    with pytest.raises(EmbeddingUnavailableError):
        _client(stub, retries=3).embed_batch(["a"])


def test_probe_dimension() -> None:
    stub = StubEndpoint()
    assert _client(stub).probe_dimension() == 4


def test_bearer_token_is_sent() -> None:
    stub = StubEndpoint()
    _client(stub).embed_batch(["a"])
    assert stub.auth_headers[-1] == "Bearer test-key"


def test_empty_input_needs_no_request() -> None:
    stub = StubEndpoint()
    assert _client(stub).embed_batch([]) == []
    assert stub.requests == []
