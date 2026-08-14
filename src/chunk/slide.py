"""Slide chunker for presentation text (one blank-line-separated block per
slide, as Tika renders it). Consecutive short slides are merged up to the
word budget; every chunk carries the deck title for context."""

import re

_SLIDE_SPLIT_RE = re.compile(r"\n\s*\n")


def chunk_slides(text: str, words: int, deck_title: str | None = None) -> list[str]:
    slides = [s.strip() for s in _SLIDE_SPLIT_RE.split(text) if s.strip()]
    if not slides:
        return []

    merged: list[str] = []
    current: list[str] = []
    current_words = 0
    for slide in slides:
        slide_words = len(slide.split())
        if current and current_words + slide_words > words:
            merged.append("\n\n".join(current))
            current = []
            current_words = 0
        current.append(slide)
        current_words += slide_words
    if current:
        merged.append("\n\n".join(current))

    if deck_title:
        return [f"{deck_title}\n\n{chunk}" for chunk in merged]
    return merged
