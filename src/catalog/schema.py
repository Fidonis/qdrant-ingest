"""Pydantic schema for ``jobs.yaml``.

Unknown keys are rejected everywhere (``extra="forbid"``) so a typo in an
operator-edited file surfaces as a named validation error instead of a
silently ignored setting.
"""

import re
from typing import Annotated, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from catalog.secrets import SecretRef

SLUG_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
COLLECTION_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$"

Slug = Annotated[str, Field(pattern=SLUG_PATTERN)]

# Payload keys owned by the ingestion contract; extra_payload may not shadow
# them because qdrant-mcp-rbac and the generation sweep depend on their values.
RESERVED_PAYLOAD_KEYS = frozenset({"text", "source", "ingest_job", "ingest_run", "acl_tags"})

_EVERY_RE = re.compile(r"^(\d+)(s|m|h|d)$")
_EVERY_FACTORS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ── Sources ───────────────────────────────────────────────────────────────────


class _SourceBase(_StrictModel):
    # Names of fields holding SecretRef values; the loader walks this to check
    # that every referenced environment variable actually exists.
    secret_fields: ClassVar[frozenset[str]] = frozenset()

    label: Slug
    rclone_flags: list[str] = Field(default_factory=list)

    def secret_env_names(self) -> dict[str, str]:
        """Map of field name -> referenced environment variable name."""
        result: dict[str, str] = {}
        for field_name in self.secret_fields:
            value = getattr(self, field_name)
            if value is not None:
                result[field_name] = value
        return result


class S3Source(_SourceBase):
    secret_fields: ClassVar[frozenset[str]] = frozenset({"access_key_id", "secret_access_key"})

    type: Literal["s3"]
    bucket: str = Field(min_length=1)
    prefix: str = ""
    provider: str = "AWS"
    region: str = ""
    endpoint: str = ""
    access_key_id: SecretRef | None = None
    secret_access_key: SecretRef | None = None


class WebdavSource(_SourceBase):
    secret_fields: ClassVar[frozenset[str]] = frozenset({"password"})

    type: Literal["webdav"]
    url: str = Field(min_length=1)
    vendor: str = "other"
    user: str = ""
    password: SecretRef | None = Field(default=None, alias="pass")


class SftpSource(_SourceBase):
    secret_fields: ClassVar[frozenset[str]] = frozenset({"password", "key_file"})

    type: Literal["sftp"]
    host: str = Field(min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    user: str = ""
    password: SecretRef | None = Field(default=None, alias="pass")
    # PEM content resolved from the environment, never a filesystem path.
    key_file: SecretRef | None = None
    path: str = "/"


class SmbSource(_SourceBase):
    secret_fields: ClassVar[frozenset[str]] = frozenset({"password"})

    type: Literal["smb"]
    host: str = Field(min_length=1)
    share: str = Field(min_length=1)
    user: str = ""
    password: SecretRef | None = Field(default=None, alias="pass")
    path: str = ""


class FtpSource(_SourceBase):
    secret_fields: ClassVar[frozenset[str]] = frozenset({"password"})

    type: Literal["ftp"]
    host: str = Field(min_length=1)
    port: int = Field(default=21, ge=1, le=65535)
    user: str = ""
    password: SecretRef | None = Field(default=None, alias="pass")
    path: str = ""
    tls: bool = False


class LocalSource(_SourceBase):
    type: Literal["local"]
    # Container-absolute path below the read-only /data/local bind mount; the
    # loader validates the prefix against the configured mount point.
    path: str = Field(min_length=1)


class GdriveSource(_SourceBase):
    secret_fields: ClassVar[frozenset[str]] = frozenset({"service_account_json", "token"})

    type: Literal["gdrive"]
    service_account_json: SecretRef | None = None
    token: SecretRef | None = None
    root_folder_id: str = ""


class AzureBlobSource(_SourceBase):
    secret_fields: ClassVar[frozenset[str]] = frozenset({"key", "sas_url"})

    type: Literal["azureblob"]
    account: str = Field(min_length=1)
    container: str = Field(min_length=1)
    key: SecretRef | None = None
    sas_url: SecretRef | None = None
    prefix: str = ""


class HttpSource(_SourceBase):
    type: Literal["http"]
    url: str = Field(min_length=1)


SourceConfig = Annotated[
    S3Source
    | WebdavSource
    | SftpSource
    | SmbSource
    | FtpSource
    | LocalSource
    | GdriveSource
    | AzureBlobSource
    | HttpSource,
    Field(discriminator="type"),
]


# ── Job sections ──────────────────────────────────────────────────────────────


class FiltersConfig(_StrictModel):
    # An empty include list means "every supported format".
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    max_file_bytes: int | None = Field(default=None, ge=1)


class TargetConfig(_StrictModel):
    collection: str = Field(pattern=COLLECTION_PATTERN)
    acl_tags: list[str] = Field(default_factory=list)
    extra_payload: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("extra_payload")
    @classmethod
    def _no_reserved_keys(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        clashes = RESERVED_PAYLOAD_KEYS.intersection(value)
        if clashes:
            raise ValueError(
                "extra_payload may not override contract fields: " + ", ".join(sorted(clashes))
            )
        return value


class ScheduleConfig(_StrictModel):
    # cron and every are mutually exclusive; both absent means manual-only.
    cron: str | None = None
    every: str | None = None
    timezone: str | None = None
    jitter_seconds: int = Field(default=30, ge=0)
    misfire_grace_seconds: int = Field(default=300, ge=0)
    run_on_startup: Literal["never", "if_missed", "always"] = "if_missed"

    @model_validator(mode="after")
    def _cron_xor_every(self) -> "ScheduleConfig":
        if self.cron is not None and self.every is not None:
            raise ValueError("schedule accepts either cron or every, not both")
        if self.every is not None and _EVERY_RE.match(self.every) is None:
            raise ValueError("every must look like '30s', '15m', '4h', or '1d'")
        return self

    @property
    def every_seconds(self) -> int | None:
        if self.every is None:
            return None
        match = _EVERY_RE.match(self.every)
        assert match is not None  # validated above
        return int(match.group(1)) * _EVERY_FACTORS[match.group(2)]

    @property
    def is_manual_only(self) -> bool:
        return self.cron is None and self.every is None


class ChunkingConfig(_StrictModel):
    strategy: Literal["auto", "markdown", "paragraph", "sheet_rows", "slide"] = "auto"
    words: int = Field(default=400, ge=1)
    overlap: int = Field(default=50, ge=0)

    @model_validator(mode="after")
    def _overlap_below_words(self) -> "ChunkingConfig":
        if self.overlap >= self.words:
            raise ValueError("chunking.overlap must be smaller than chunking.words")
        return self


class EmbeddingConfig(_StrictModel):
    # None falls back to QI_EMBEDDING_MODEL / QI_EMBED_BATCH_SIZE at load time.
    model: str | None = None
    batch_size: int | None = Field(default=None, ge=1)


class SafetyConfig(_StrictModel):
    max_delete_ratio: float = Field(default=0.25, ge=0.0, le=1.0)
    empty_source_guard: bool = True


class JobConfig(_StrictModel):
    id: Slug
    enabled: bool = True
    description: str = ""
    source: SourceConfig
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    target: TargetConfig
    mode: Literal["full", "append", "upsert"]
    full_scope: Literal["job", "collection"] = "job"
    append_probe: Literal["auto", "state", "qdrant"] = "auto"
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    mcp_allow_full: bool = False
    expand_embedded: bool = False
    source_template: str = "{scheme}://{label}/{rel_path}"

    @field_validator("source_template")
    @classmethod
    def _template_placeholders(cls, value: str) -> str:
        if "{rel_path}" not in value or "{label}" not in value:
            raise ValueError("source_template must contain {label} and {rel_path}")
        return value

    def source_uri(self, rel_path: str) -> str:
        """Canonical payload ``source`` value for a file of this job."""
        return self.source_template.format(
            scheme=self.source.type, label=self.source.label, rel_path=rel_path
        )

    def source_prefix(self) -> str:
        """The template rendered with an empty rel_path — the job's URI prefix."""
        return self.source_uri("")
