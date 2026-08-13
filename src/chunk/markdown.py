"""Heading-aware markdown chunking.

Sections are split on heading starts with a zero-width lookahead so each
heading stays attached to its body; oversized sections fall back to a word
window, with the heading re-prepended to every sub-chunk so each chunk keeps
its context. Markdown must never travel through Tika — its plaintext handler
strips the ``#`` markers this chunker depends on.
"""

import re

_HEADING_SPLIT_RE = re.compile(r"(?m)^(?=#{1,6} )")
_FIRST_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)


def extract_markdown_title(text: str) -> str | None:
    """The first heading, if any."""
    match = _FIRST_HEADING_RE.search(text)
    return match.group(1).strip() if match else None


def chunk_markdown(text: str, words: int, overlap: int) -> list[str]:
    """Split on headings, fall back to a word window inside long sections."""
    raw_sections = [s.strip() for s in _HEADING_SPLIT_RE.split(text) if s.strip()]

    chunks: list[str] = []
    for section in raw_sections:
        section_words = section.split()
        if len(section_words) <= words:
            chunks.append(section)
            continue

        first_line, _, body = section.partition("\n")
        if first_line.startswith("#"):
            heading = first_line.strip()
            heading_words = heading.split()
            body_words = body.split()
        else:
            heading = ""
            heading_words = []
            body_words = section_words

        max_body = max(1, words - len(heading_words))
        step = max(1, max_body - overlap)
        for i in range(0, len(body_words), step):
            sub_words = body_words[i : i + max_body]
            if not sub_words:
                continue
            chunk = (heading + "\n\n" + " ".join(sub_words)) if heading else " ".join(sub_words)
            chunks.append(chunk)

    if not chunks:
        body_words = text.split()
        step = max(1, words - overlap)
        return [
            " ".join(body_words[i : i + words])
            for i in range(0, len(body_words), step)
            if body_words[i : i + words]
        ]
    return chunks
