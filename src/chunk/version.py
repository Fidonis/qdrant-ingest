"""Chunker version marker.

Bump on any change that alters chunk boundaries or chunk text for unchanged
input. The value feeds params_sha, so a bump forces a clean re-embedding of
every document on its next run — no manual full reindex needed.
"""

CHUNKER_VERSION = 1
