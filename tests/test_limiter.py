"""Embedding concurrency and rate limiting."""

import threading
import time

from embed import EmbeddingLimiter, LimitedEmbedder

from fakes.embeddings import FakeEmbeddings


def test_semaphore_bounds_concurrency() -> None:
    limiter = EmbeddingLimiter(concurrency=2)
    active = 0
    peak = 0
    guard = threading.Lock()

    def worker() -> None:
        nonlocal active, peak
        with limiter.slot():
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with guard:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert peak <= 2


def test_token_bucket_paces_requests() -> None:
    class Clock:
        def __init__(self) -> None:
            self.now = 0.0
            self.sleeps: list[float] = []

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)
            self.now += seconds

    clock = Clock()
    limiter = EmbeddingLimiter(
        concurrency=8, rps=2.0, monotonic=clock.monotonic, sleep=clock.sleep
    )

    for _ in range(4):
        with limiter.slot():
            pass

    # Capacity 2: the first two pass immediately, then one token per 0.5s.
    assert len(clock.sleeps) == 2
    assert all(abs(wait - 0.5) < 1e-6 for wait in clock.sleeps)


def test_rps_zero_never_sleeps() -> None:
    sleeps: list[float] = []
    limiter = EmbeddingLimiter(concurrency=1, rps=0.0, sleep=sleeps.append)
    for _ in range(10):
        with limiter.slot():
            pass
    assert sleeps == []


def test_limited_embedder_passes_through() -> None:
    inner = FakeEmbeddings(dimension=4)
    limited = LimitedEmbedder(inner, EmbeddingLimiter(concurrency=2))
    assert limited.probe_dimension() == 4
    vectors = limited.embed_all([f"t{i}" for i in range(5)], batch_size=2)
    assert len(vectors) == 5
    assert inner.calls == 3  # 2 + 2 + 1
    assert vectors[0] == inner.vector_for("t0")
