"""The ingestion engine: run execution, mode semantics, guards, and locks."""

from embed.client import EmbedderProtocol
from engine.guards import GuardDecision, check_vanished_deletion
from engine.locks import LockingRunner, RunRejectedError
from engine.modes import job_params_sha, sha256_bytes, sha256_file
from engine.runner import JobRunner

__all__ = [
    "EmbedderProtocol",
    "GuardDecision",
    "JobRunner",
    "LockingRunner",
    "RunRejectedError",
    "check_vanished_deletion",
    "job_params_sha",
    "sha256_bytes",
    "sha256_file",
]
