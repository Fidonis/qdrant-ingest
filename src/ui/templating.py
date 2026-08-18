"""The Jinja2 environment for the web interface.

fidonis-brand: 1 -- the asset fingerprinting below is part of the vendored
brand layer shared with papaia-manager. Keep the two in step; see
docs/ui.md.
"""

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates

_UI_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _UI_DIR / "static"

templates = Jinja2Templates(directory=str(_UI_DIR / "templates"))

# name -> (st_mtime_ns, st_size, url)
_ASSET_URLS: dict[str, tuple[int, int, str]] = {}

# Filled in at application build time; the mount point is derived from the
# configured ui_path, so the templates cannot hardcode it.
_ASSET_PREFIX = "/ui/static"


def set_asset_prefix(prefix: str) -> None:
    global _ASSET_PREFIX
    _ASSET_PREFIX = prefix.rstrip("/")
    _ASSET_URLS.clear()


def asset_url(name: str) -> str:
    """Return a static asset's URL carrying a fingerprint of its content.

    ``app.css`` is generated *from the templates* at image build time, so a
    copy left in a browser cache pairs new markup with an older build's
    styling and silently drops whatever that markup relies on. Only a change
    of URL evicts it: the static mount sends no Cache-Control, so browsers
    fall back to heuristic freshness and reuse the response without
    revalidating at all -- an ETag never gets a chance to be checked.

    Keyed on the stat signature, so an asset rebuilt under a running process
    is picked up without a restart and re-hashed only when that moves.
    """
    path = _STATIC_DIR / name
    try:
        stat = path.stat()
    except OSError:
        return f"{_ASSET_PREFIX}/{name}"

    cached = _ASSET_URLS.get(name)
    if cached is not None and (cached[0], cached[1]) == (stat.st_mtime_ns, stat.st_size):
        return cached[2]

    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return f"{_ASSET_PREFIX}/{name}"

    url = f"{_ASSET_PREFIX}/{name}?v={digest[:12]}"
    _ASSET_URLS[name] = (stat.st_mtime_ns, stat.st_size, url)
    return url


def _short(value: Any, length: int = 12) -> str:
    """Truncate an identifier for display, keeping the front."""
    text = "" if value is None else str(value)
    return text if len(text) <= length else text[:length] + "…"


def _format_seconds(total: float) -> str:
    """Render a duration the way an operator scans it: 1m 04s, not 64.2."""
    if total < 60:
        return f"{total:.1f}s"
    minutes, rest = divmod(int(total), 60)
    if minutes < 60:
        return f"{minutes}m {rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _duration(run: Any) -> str:
    """How long a run took, or has been running, from its RunRow mapping.

    RunRow (state/models.py) carries no duration field of its own -- only
    ``started_at`` and ``finished_at`` timestamps, produced by
    ``state.db.now_iso()``. A run still in progress has no ``finished_at``
    yet; its elapsed time is measured against now instead, which is what
    makes a `status: running` row show a climbing duration rather than a
    blank one.
    """
    if not isinstance(run, Mapping):
        return "—"
    started = _parse_iso(run.get("started_at"))
    if started is None:
        return "—"
    finished = _parse_iso(run.get("finished_at"))
    ended = finished if finished is not None else datetime.now(UTC)
    return _format_seconds((ended - started).total_seconds())


templates.env.globals["asset_url"] = asset_url
templates.env.filters["short"] = _short
templates.env.filters["duration"] = _duration
