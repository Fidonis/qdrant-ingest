"""Text extraction: Tika client, native readers, and the format router."""

from extract.router import ProcessedFile, classify, process_file
from extract.tika import ExtractionResult, TikaClient, TikaError

__all__ = [
    "ExtractionResult",
    "ProcessedFile",
    "TikaClient",
    "TikaError",
    "classify",
    "process_file",
]
