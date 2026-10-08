"""Request bodies of the REST control plane."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The catalog text to check. A real catalog is a few kilobytes; the cap only keeps a
    # careless client from posting something that makes the parser work for seconds.
    raw: str = Field(max_length=2_000_000)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["full", "append", "upsert"] | None = None
    full_scope: Literal["job", "collection"] | None = None
    dry_run: bool = False
    skip_sync: bool = False
    force: bool = False
    queue: bool = False
    # `false` skips the deletion phase of an `upsert` run: documents are added and
    # replaced, nothing that is missing from the scan is removed.
    delete_vanished: bool = True
