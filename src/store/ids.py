"""Deterministic chunk point ids.

This namespace is new — deliberately not the seed string of any earlier
ingestion service. Point ids here are purely an *overwrite key* so re-embeds
replace cleanly: every delete path (per-document delete_by, the generation
sweep, orphan cleanup) filters on payload fields and never looks points up by
id. Changing the id derivation therefore cannot strand vectors, which is
exactly the hazard the classic "never change the id seed" warning guards
against; it does not apply to this design.

``job_id`` is part of the id so that two jobs which end up producing the same
``source`` value in the same collection (bypassing catalog validation) still
cannot overwrite each other's points.
"""

import uuid

_INGEST_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "de.fidonis/qdrant-ingest")


def point_id(job_id: str, source: str, chunk_index: int) -> str:
    return str(uuid.uuid5(_INGEST_NAMESPACE, f"{job_id}|{source}#{chunk_index}"))
