"""OpenAI-shaped embeddings stub for the functional-test stack.

Standard library only, so it runs on an unmodified python:3.12-slim image
with a single bind-mounted file. Vectors are deterministic (seeded from the
text), the response `data` is returned in *reversed* order on purpose — the
client must re-sort by `index` — and `/stats` exposes request and text
counters, which the driver uses to prove that unchanged documents are never
re-embedded.
"""

import hashlib
import json
import math
import os
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIMENSION = int(os.environ.get("EMBED_DIM", "768"))
PORT = int(os.environ.get("PORT", "8090"))

_lock = threading.Lock()
_stats = {"requests": 0, "texts": 0}


def vector_for(text: str) -> list[float]:
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    raw = [rng.uniform(-1.0, 1.0) for _ in range(DIMENSION)]
    norm = math.sqrt(sum(v * v for v in raw)) or 1.0
    return [v / norm for v in raw]


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path.endswith("/stats"):
            with _lock:
                self._send_json(200, dict(_stats))
        elif self.path.endswith("/models"):
            self._send_json(200, {"object": "list", "data": []})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        if self.path.endswith("/stats/reset"):
            with _lock:
                _stats["requests"] = 0
                _stats["texts"] = 0
            self._send_json(200, dict(_stats))
            return
        if not self.path.endswith("/embeddings"):
            self._send_json(404, {"error": "not found"})
            return
        try:
            body = json.loads(raw)
        except ValueError:
            self._send_json(400, {"error": "invalid json"})
            return
        texts = body.get("input", [])
        if isinstance(texts, str):
            texts = [texts]
        with _lock:
            _stats["requests"] += 1
            _stats["texts"] += len(texts)
        data = [
            {"object": "embedding", "index": i, "embedding": vector_for(text)}
            for i, text in enumerate(texts)
        ]
        data.reverse()  # deliberate: the client must sort by index
        self._send_json(
            200,
            {
                "object": "list",
                "model": body.get("model", "stub-embed"),
                "data": data,
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            },
        )

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # keep the container logs quiet


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
