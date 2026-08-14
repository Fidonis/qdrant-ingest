"""The ingestion engine: run execution, mode semantics, and guards."""

from engine.guards import GuardDecision, check_vanished_deletion
from engine.modes import job_params_sha, sha256_bytes, sha256_file
from engine.runner import EmbedderProtocol, JobRunner

__all__ = [
    "EmbedderProtocol",
    "GuardDecision",
    "JobRunner",
    "check_vanished_deletion",
    "job_params_sha",
    "sha256_bytes",
    "sha256_file",
]
