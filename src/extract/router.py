"""Routing: (extension, Tika media type) → extraction path → chunker.

The extension wins when both are known; the Tika-detected Content-Type is the
routing key only for unknown extensions (behind QI_TIKA_SNIFF_UNKNOWN).
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from catalog.schema import ChunkingConfig
from chunk import chunk_markdown, chunk_paragraphs, chunk_slides, chunk_spreadsheet_text
from chunk.markdown import extract_markdown_title
from config import Settings
from extract.native import native_media_type, read_data_file, read_text_file
from extract.tika import EmbeddedDocument, TikaClient

FormatClass = Literal[
    "markdown",
    "plaintext",
    "data",
    "document",
    "spreadsheet_tika",
    "spreadsheet_text",
    "presentation",
    "web",
    "email",
    "image",
    "archive",
    "unknown",
]

_EXTENSION_CLASSES: dict[str, FormatClass] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".mdx": "markdown",
    ".txt": "plaintext",
    ".log": "plaintext",
    ".rst": "plaintext",
    ".adoc": "plaintext",
    ".json": "data",
    ".yaml": "data",
    ".yml": "data",
    ".pdf": "document",
    ".doc": "document",
    ".docx": "document",
    ".odt": "document",
    ".rtf": "document",
    ".epub": "document",
    ".xlsx": "spreadsheet_tika",
    ".xls": "spreadsheet_tika",
    ".ods": "spreadsheet_tika",
    ".csv": "spreadsheet_text",
    ".tsv": "spreadsheet_text",
    ".pptx": "presentation",
    ".ppt": "presentation",
    ".odp": "presentation",
    ".html": "web",
    ".htm": "web",
    ".xml": "web",
    ".eml": "email",
    ".msg": "email",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".tiff": "image",
    ".zip": "archive",
    ".tar": "archive",
    ".gz": "archive",
    ".tgz": "archive",
    ".7z": "archive",
}

_NATIVE_CLASSES: frozenset[FormatClass] = frozenset(
    {"markdown", "plaintext", "data", "spreadsheet_text"}
)

_MEDIA_TYPE_CLASSES: tuple[tuple[str, FormatClass], ...] = (
    ("application/pdf", "document"),
    ("wordprocessingml", "document"),
    ("msword", "document"),
    ("opendocument.text", "document"),
    ("rtf", "document"),
    ("epub", "document"),
    ("spreadsheetml", "spreadsheet_tika"),
    ("ms-excel", "spreadsheet_tika"),
    ("opendocument.spreadsheet", "spreadsheet_tika"),
    ("presentationml", "presentation"),
    ("ms-powerpoint", "presentation"),
    ("opendocument.presentation", "presentation"),
    ("text/html", "web"),
    ("xml", "web"),
    ("message/rfc822", "email"),
    ("image/", "image"),
    ("text/markdown", "markdown"),
    ("text/csv", "spreadsheet_text"),
    ("text/", "plaintext"),
)


def classify(rel_path: str) -> FormatClass:
    suffix = Path(rel_path).suffix.lower()
    return _EXTENSION_CLASSES.get(suffix, "unknown")


def class_for_media_type(media_type: str | None) -> FormatClass:
    if not media_type:
        return "unknown"
    lowered = media_type.lower()
    for needle, format_class in _MEDIA_TYPE_CLASSES:
        if needle in lowered:
            return format_class
    return "unknown"


@dataclass
class ProcessedFile:
    """Outcome of extraction plus chunking for one file."""

    status: Literal["ok", "no_text", "unsupported"]
    chunks: list[str] = field(default_factory=list)
    text: str = ""
    media_type: str | None = None
    title: str = ""
    format_class: FormatClass = "unknown"
    embedded: list[EmbeddedDocument] = field(default_factory=list)


def _chunk_for(
    format_class: FormatClass,
    text: str,
    title: str,
    chunking: ChunkingConfig,
    settings: Settings,
) -> list[str]:
    strategy = chunking.strategy
    if strategy == "auto":
        if format_class == "markdown":
            strategy = "markdown"
        elif format_class in {"spreadsheet_tika", "spreadsheet_text"}:
            strategy = "sheet_rows"
        elif format_class == "presentation":
            strategy = "slide"
        else:
            strategy = "paragraph"

    if strategy == "markdown":
        return chunk_markdown(text, chunking.words, chunking.overlap)
    if strategy == "sheet_rows":
        return chunk_spreadsheet_text(text, settings.sheet_rows)
    if strategy == "slide":
        return chunk_slides(text, chunking.words, deck_title=title or None)
    return chunk_paragraphs(text, chunking.words, chunking.overlap)


def process_file(
    path: Path,
    rel_path: str,
    chunking: ChunkingConfig,
    settings: Settings,
    tika: TikaClient,
) -> ProcessedFile:
    """Extract and chunk one file. TikaError propagates to the caller, which
    records the document as failed_extract."""
    format_class = classify(rel_path)

    if format_class == "archive":
        return ProcessedFile(status="unsupported", format_class=format_class)

    media_type: str | None = None
    title: str | None = None
    pages: int | None = None
    embedded: list[EmbeddedDocument] = []

    if format_class == "unknown":
        if not settings.tika_sniff_unknown:
            return ProcessedFile(status="unsupported", format_class=format_class)
        result = tika.extract(path, rel_path.rsplit("/", 1)[-1])
        sniffed = class_for_media_type(result.media_type)
        if sniffed in {"unknown", "archive"}:
            return ProcessedFile(
                status="unsupported", media_type=result.media_type, format_class="unknown"
            )
        format_class = sniffed
        text = result.text
        media_type = result.media_type
        title = result.title
        pages = result.pages
        embedded = result.embedded
    elif format_class in _NATIVE_CLASSES:
        text = (
            read_data_file(path) if format_class == "data" else read_text_file(path)
        )
        media_type = native_media_type(path.suffix)
    else:
        result = tika.extract(path, rel_path.rsplit("/", 1)[-1])
        text = result.text
        media_type = result.media_type
        title = result.title
        pages = result.pages
        embedded = result.embedded

    resolved_title = title or extract_markdown_title(text) or Path(rel_path).stem

    if not text.strip():
        return ProcessedFile(
            status="no_text",
            media_type=media_type,
            title=resolved_title,
            format_class=format_class,
            embedded=embedded,
        )

    # Scanned-PDF heuristic: hardly any text per page means the content is
    # pixels, not glyphs. Only OCR (the -full Tika image) could change that.
    if (
        format_class == "document"
        and media_type is not None
        and "pdf" in media_type.lower()
        and len(text) / max(1, pages or 1) < settings.min_chars_per_page
    ):
        return ProcessedFile(
            status="no_text",
            text=text,
            media_type=media_type,
            title=resolved_title,
            format_class=format_class,
        )

    chunks = _chunk_for(format_class, text, resolved_title, chunking, settings)
    if not chunks:
        return ProcessedFile(
            status="no_text",
            text=text,
            media_type=media_type,
            title=resolved_title,
            format_class=format_class,
            embedded=embedded,
        )

    return ProcessedFile(
        status="ok",
        chunks=chunks,
        text=text,
        media_type=media_type,
        title=resolved_title,
        format_class=format_class,
        embedded=embedded,
    )
