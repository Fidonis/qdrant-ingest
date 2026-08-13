"""In-process Tika stand-in served through httpx.MockTransport.

Counts every request — the router tests use the counter to prove that
markdown never travels through Tika.
"""

from typing import Any

import httpx


class FakeTika:
    def __init__(self) -> None:
        self.calls = 0
        self.filenames: list[str] = []
        self.responses: dict[str, list[dict[str, Any]]] = {}
        self.default_elements: list[dict[str, Any]] = [
            {"X-TIKA:content": "extracted text", "Content-Type": "application/pdf"}
        ]
        # Forced outcomes, consumed one per request before canned responses.
        self.status_queue: list[int] = []
        self.connect_errors: int = 0

    def set_response(self, filename: str, elements: list[dict[str, Any]]) -> None:
        self.responses[filename] = elements

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        disposition = request.headers.get("Content-Disposition", "")
        filename = disposition.split('filename="')[-1].rstrip('"')
        self.filenames.append(filename)
        if self.connect_errors > 0:
            self.connect_errors -= 1
            raise httpx.ConnectError("fake connection refused", request=request)
        if self.status_queue:
            return httpx.Response(self.status_queue.pop(0), text="forced error")
        elements = self.responses.get(filename, self.default_elements)
        return httpx.Response(200, json=elements)
