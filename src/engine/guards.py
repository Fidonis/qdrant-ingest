"""Safety guards for the deletion phase.

rclone's --max-delete is the first line of defense; these checks are the
second, because the tree can also be empty for reasons rclone never sees
(wrong path, unmounted volume, a typo in the prefix).
"""

from dataclasses import dataclass

from catalog.schema import SafetyConfig


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reason: str | None = None


def check_vanished_deletion(
    seen: set[str],
    state_sources: set[str],
    safety: SafetyConfig,
    force: bool,
) -> GuardDecision:
    """May this run delete the sources that vanished from the tree?"""
    vanished = state_sources - seen
    if not vanished:
        return GuardDecision(allowed=True)
    if safety.empty_source_guard and not seen and state_sources:
        return GuardDecision(
            allowed=False,
            reason=(
                "the scanned tree is empty but state tracks "
                f"{len(state_sources)} documents; refusing to delete anything"
            ),
        )
    ratio = len(vanished) / max(1, len(state_sources))
    if ratio > safety.max_delete_ratio and not force:
        return GuardDecision(
            allowed=False,
            reason=(
                f"run would delete {len(vanished)} of {len(state_sources)} documents "
                f"({ratio:.0%}), above max_delete_ratio {safety.max_delete_ratio:.0%}; "
                "re-run with force to override"
            ),
        )
    return GuardDecision(allowed=True)
