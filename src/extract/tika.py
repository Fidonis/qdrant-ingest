"""Client for Apache Tika's ``PUT /rmeta/text``.

/rmeta/text (not /tika) because the JSON envelope carries what plain text
cannot: the detected Content-Type (routing key for unknown extensions),
``dc:title``, per-embedded-document elements for containers and mail, and
``X-TIKA:EXCEPTION:*`` — the only way to tell a partially failed parse from a
genuinely empty document (/tika returns 200 with an empty body for both).

The request body is streamed; a large PDF must never be read into the
ingester's heap.
"""

import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

_RETRY_DELAYS = (0.5, 1.0)  # two retries with backoff on 5xx / connect errors


class TikaError(Exception):
    """Extraction failed. ``terminal`` means retrying cannot help until the
    file itself changes (unsupported, encrypted, malformed)."""

    def __init__(self, message: str, *, terminal: bool) -> None:
        super().__init__(message)
        self.terminal = terminal


@dataclass
class EmbeddedDocument:
    name: str
    text: str
    media_type: str | None


@dataclass
class ExtractionResult:
    text: str
    media_type: str | None
    title: str | None
    pages: int | None
    exceptions: list[str] = field(default_factory=list)
    embedded: list[EmbeddedDocument] = field(default_factory=list)


def _ascii_filename(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    cleaned = "".join(c for c in normalized if c.isprintable() and c not in '"\\')
    return cleaned or "file"


def _first_str(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    if isinstance(value, list):
        value = value[0] if value else None
    return value if isinstance(value, str) else None


class TikaClient:
    """Synchronous Tika client with bounded retries."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 300.0,
        connect_timeout: float = 5.0,
        ocr_language: str = "deu+eng",
        pdf_ocr_strategy: str = "auto",
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            transport=transport,
        )
        self._ocr_language = ocr_language
        self._pdf_ocr_strategy = pdf_ocr_strategy
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def ping(self) -> bool:
        """Reachability probe for /health dependency reporting."""
        try:
            response = self._client.get("/tika")
            return response.status_code < 500
        except httpx.HTTPError:
            return False

    def extract(self, path: Path, filename: str | None = None) -> ExtractionResult:
        name = _ascii_filename(filename or path.name)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Tika-PDFOcrStrategy": self._pdf_ocr_strategy,
            "X-Tika-OCRLanguage": self._ocr_language,
            "X-Tika-PDFextractInlineImages": "false",
        }

        last_error: Exception | None = None
        for attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                with path.open("rb") as body:
                    response = self._client.put("/rmeta/text", content=body, headers=headers)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < len(_RETRY_DELAYS):
                    self._sleep(_RETRY_DELAYS[attempt])
                    continue
                raise TikaError(f"tika unreachable: {exc}", terminal=False) from exc

            if response.status_code == 422:
                raise TikaError(
                    "tika cannot parse this file (unsupported or encrypted)", terminal=True
                )
            if 400 <= response.status_code < 500:
                raise TikaError(
                    f"tika rejected the request with {response.status_code}", terminal=True
                )
            if response.status_code >= 500:
                last_error = httpx.HTTPStatusError(
                    f"tika returned {response.status_code}",
                    request=response.request,
                    response=response,
                )
                if attempt < len(_RETRY_DELAYS):
                    self._sleep(_RETRY_DELAYS[attempt])
                    continue
                raise TikaError(
                    f"tika kept failing with {response.status_code}", terminal=False
                ) from last_error

            return self._parse(response)

        raise TikaError(f"tika unreachable: {last_error}", terminal=False)  # pragma: no cover

    def _parse(self, response: httpx.Response) -> ExtractionResult:
        try:
            elements = response.json()
        except ValueError as exc:
            raise TikaError("tika returned invalid JSON", terminal=False) from exc
        if not isinstance(elements, list) or not elements:
            raise TikaError("tika returned an empty envelope", terminal=False)

        container = elements[0]
        if not isinstance(container, dict):
            raise TikaError("tika returned a malformed envelope", terminal=False)

        text = container.get("X-TIKA:content") or ""
        media_type = _first_str(container, "Content-Type")
        title = _first_str(container, "dc:title")
        pages_raw = _first_str(container, "xmpTPg:NPages")
        try:
            pages = int(pages_raw) if pages_raw else None
        except ValueError:
            pages = None

        exceptions = [
            f"{key}: {value}"
            for key, value in container.items()
            if key.startswith("X-TIKA:EXCEPTION")
        ]

        embedded: list[EmbeddedDocument] = []
        for element in elements[1:]:
            if not isinstance(element, dict):
                continue
            embedded_name = (
                _first_str(element, "resourceName")
                or _first_str(element, "X-TIKA:embedded_resource_path")
                or f"embedded-{len(embedded) + 1}"
            )
            embedded.append(
                EmbeddedDocument(
                    name=embedded_name.lstrip("/"),
                    text=element.get("X-TIKA:content") or "",
                    media_type=_first_str(element, "Content-Type"),
                )
            )

        return ExtractionResult(
            text=text.strip(),
            media_type=media_type,
            title=title.strip() if title else None,
            pages=pages,
            exceptions=exceptions,
            embedded=embedded,
        )
