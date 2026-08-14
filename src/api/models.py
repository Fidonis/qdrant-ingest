"""Request bodies of the REST control plane."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["full", "append", "upsert"] | None = None
    full_scope: Literal["job", "collection"] | None = None
    dry_run: bool = False
    skip_sync: bool = False
    force: bool = False
    queue: bool = False
