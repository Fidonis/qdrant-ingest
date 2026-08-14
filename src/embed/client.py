"""Batch client for an OpenAI-compatible ``/embeddings`` endpoint.

Plain httpx — the endpoint contract is one POST route, and the response
``data`` is re-sorted by ``index`` because providers may return embeddings
out of order.
"""

import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx


class EmbeddingUnavailableError(Exception):
    """The embeddings endpoint kept failing; the run must abort."""


class EmbedderProtocol(Protocol):
    """What the engine needs from any embedder implementation."""

    def probe_dimension(self) -> int: ...

    def embed_all(self, texts: list[str], batch_size: int) -> list[list[float]]: ...


class EmbeddingClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        retries: int = 3,
        retry_delay: float = 5.0,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )
        self._model = model
        self._retries = max(1, retries)
        self._retry_delay = retry_delay
        self._sleep = sleep

    @property
    def model(self) -> str:
        return self._model

    def close(self) -> None:
        self._client.close()

    def ping(self) -> bool:
        """Reachability probe for /health dependency reporting."""
        try:
            response = self._client.get("/models")
            return response.status_code < 500
        except httpx.HTTPError:
            return False

    def _request_batch(self, texts: list[str]) -> list[list[float]]:
        response = self._client.post(
            "/embeddings", json={"model": self._model, "input": texts}
        )
        response.raise_for_status()
        payload: Any = response.json()
        data = payload.get("data")
        if not isinstance(data, list):
            raise EmbeddingUnavailableError("embeddings response has no data array")
        # Re-sort by index: the order of `data` is not guaranteed.
        ordered = sorted(data, key=lambda item: item.get("index", 0))
        return [list(item["embedding"]) for item in ordered]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """One batch with bounded retries; raises EmbeddingUnavailableError."""
        if not texts:
            return []
        last_error: Exception | None = None
        for attempt in range(1, self._retries + 1):
            try:
                return self._request_batch(texts)
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                last_error = exc
                if attempt < self._retries:
                    self._sleep(self._retry_delay)
        raise EmbeddingUnavailableError(
            f"embeddings endpoint failed after {self._retries} attempts: {last_error}"
        ) from last_error

    def embed_all(self, texts: list[str], batch_size: int) -> list[list[float]]:
        """All texts, in api-sized batches, order-preserving."""
        vectors: list[list[float]] = []
        step = max(1, batch_size)
        for start in range(0, len(texts), step):
            vectors.extend(self.embed_batch(texts[start : start + step]))
        return vectors

    def probe_dimension(self) -> int:
        """Learn the vector size once per model."""
        vectors = self.embed_batch(["dimension-probe"])
        if not vectors or not vectors[0]:
            raise EmbeddingUnavailableError("dimension probe returned no vector")
        return len(vectors[0])
