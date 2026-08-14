"""Deterministic embedding fake with call and text counters.

The counters are the instrument for change-detection assertions: a stage-1/2/3
skip must show up as a zero delta on ``texts_embedded``.
"""

import hashlib
import math
import random

from embed.client import EmbeddingUnavailableError


class FakeEmbeddings:
    def __init__(self, dimension: int = 8) -> None:
        self.dimension = dimension
        self.calls = 0
        self.texts_embedded: list[str] = []
        self.fail = False
        # When set, embed_all raises once this many texts have been embedded.
        self.fail_after_texts: int | None = None

    def vector_for(self, text: str) -> list[float]:
        seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
        rng = random.Random(seed)
        raw = [rng.uniform(-1.0, 1.0) for _ in range(self.dimension)]
        norm = math.sqrt(sum(component * component for component in raw)) or 1.0
        return [component / norm for component in raw]

    def probe_dimension(self) -> int:
        if self.fail:
            raise EmbeddingUnavailableError("embedding endpoint down")
        return self.dimension

    def embed_all(self, texts: list[str], batch_size: int) -> list[list[float]]:
        if self.fail:
            raise EmbeddingUnavailableError("embedding endpoint down")
        if (
            self.fail_after_texts is not None
            and len(self.texts_embedded) >= self.fail_after_texts
        ):
            raise EmbeddingUnavailableError("embedding endpoint died mid-run")
        self.calls += 1
        self.texts_embedded.extend(texts)
        return [self.vector_for(text) for text in texts]
