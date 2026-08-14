"""Embedding-side rate limiting, independent of job parallelism.

The embeddings endpoint is one shared bottleneck no matter how many jobs run:
a global semaphore bounds concurrent requests, and an optional token bucket
(QI_EMBED_RPS, 0 = off) paces metered cloud models. Four jobs may sync and
extract in parallel while only two embed.
"""

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from embed.client import EmbedderProtocol


class EmbeddingLimiter:
    def __init__(
        self,
        concurrency: int,
        rps: float = 0.0,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._semaphore = threading.BoundedSemaphore(max(1, concurrency))
        self._rps = rps
        self._monotonic = monotonic
        self._sleep = sleep
        self._bucket_lock = threading.Lock()
        self._allowance = max(1.0, rps)
        self._capacity = max(1.0, rps)
        self._last_refill = monotonic()

    def _take_token(self) -> None:
        if self._rps <= 0:
            return
        while True:
            with self._bucket_lock:
                now = self._monotonic()
                self._allowance = min(
                    self._capacity,
                    self._allowance + (now - self._last_refill) * self._rps,
                )
                self._last_refill = now
                if self._allowance >= 1.0:
                    self._allowance -= 1.0
                    return
                wait = (1.0 - self._allowance) / self._rps
            self._sleep(wait)

    @contextmanager
    def slot(self) -> Iterator[None]:
        with self._semaphore:
            self._take_token()
            yield


class LimitedEmbedder:
    """EmbedderProtocol wrapper that routes every call through the limiter."""

    def __init__(self, embedder: EmbedderProtocol, limiter: EmbeddingLimiter) -> None:
        self._embedder = embedder
        self._limiter = limiter

    def probe_dimension(self) -> int:
        with self._limiter.slot():
            return self._embedder.probe_dimension()

    def embed_all(self, texts: list[str], batch_size: int) -> list[list[float]]:
        step = max(1, batch_size)
        vectors: list[list[float]] = []
        for start in range(0, len(texts), step):
            with self._limiter.slot():
                vectors.extend(
                    self._embedder.embed_all(texts[start : start + step], step)
                )
        return vectors
