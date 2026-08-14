"""Paragraph-packing chunker with a word-window fallback for long paragraphs."""

import re

_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")


def _word_window(word_list: list[str], words: int, overlap: int) -> list[str]:
    step = max(1, words - overlap)
    return [
        " ".join(word_list[i : i + words])
        for i in range(0, len(word_list), step)
        if word_list[i : i + words]
    ]


def chunk_paragraphs(text: str, words: int, overlap: int) -> list[str]:
    """Pack whole paragraphs up to the word budget; window oversized ones."""
    paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT_RE.split(text) if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_words = 0

    def flush() -> None:
        nonlocal current, current_words
        if current:
            chunks.append("\n\n".join(current))
            current = []
            current_words = 0

    for paragraph in paragraphs:
        paragraph_words = paragraph.split()
        if len(paragraph_words) > words:
            flush()
            chunks.extend(_word_window(paragraph_words, words, overlap))
            continue
        if current_words + len(paragraph_words) > words:
            flush()
        current.append(paragraph)
        current_words += len(paragraph_words)

    flush()
    return chunks
