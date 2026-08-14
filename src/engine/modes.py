"""Hashes that drive the four-stage change detection.

Stage 1 compares (size, mtime_ns) — zero I/O. Stage 2 compares the file
hash, catching touch-only changes (a cache-volume loss resets every mtime;
without this stage that would re-embed the entire corpus). Stage 3 compares
the extracted-text hash, catching binary-level rewrites that leave the text
identical. Stage 4 — the params hash below — forces a re-embed whenever any
parameter that shapes chunks or vectors changes, no matter what stages 1–3
say.
"""

import hashlib
from pathlib import Path

from catalog.schema import JobConfig
from chunk import CHUNKER_VERSION
from config import Settings


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def job_params_sha(job: JobConfig, settings: Settings) -> str:
    """Everything that changes chunk boundaries or vectors for unchanged input."""
    model = job.embedding.model or settings.embedding_model
    parts = (
        model,
        job.chunking.strategy,
        str(job.chunking.words),
        str(job.chunking.overlap),
        str(CHUNKER_VERSION),
        settings.tika_ocr_language,
        settings.tika_pdf_ocr_strategy,
        str(settings.tika_sniff_unknown),
        str(settings.sheet_rows),
        str(settings.min_chars_per_page),
        str(job.expand_embedded),
    )
    return sha256_bytes("|".join(parts).encode("utf-8"))
