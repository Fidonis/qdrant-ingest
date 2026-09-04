"""Pydantic schema for ``connections.yaml``.

One file declares every Qdrant a job may target. Unknown keys are rejected
(``extra="forbid"``) so a typo surfaces as a named error rather than a silently
ignored setting.

The ``api_key`` field never holds a plaintext secret. It is either absent (an
unauthenticated Qdrant) or an ``enc:1:`` token written by the web interface and
decrypted at load time -- a literal value is refused so a key cannot be pasted
in by hand and left on disk in the clear.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from connections.crypto import ENC_PREFIX

# Same shape as a job id: lowercase, digits, dash and underscore.
CONNECTION_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"

ConnectionName = Annotated[str, Field(pattern=CONNECTION_NAME_PATTERN)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ConnectionConfig(_StrictModel):
    name: ConnectionName
    url: str = Field(min_length=1)
    api_key: str | None = None

    @field_validator("url")
    @classmethod
    def _http_scheme(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return value

    @field_validator("api_key")
    @classmethod
    def _encrypted_only(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not value.startswith(ENC_PREFIX):
            raise ValueError(
                "api_key must be set through the web interface; a literal value "
                "is not stored in connections.yaml"
            )
        return value
