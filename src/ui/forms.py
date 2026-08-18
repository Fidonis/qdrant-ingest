"""Turning the catalog schema into a form, and a form back into a job.

The field list is derived from the Pydantic source models rather than typed
out again, so a field added to :mod:`catalog.schema` appears in the form
without anyone remembering to come here.

One hazard shapes this whole module. After validation a ``SecretRef`` field
holds the *environment variable name*, not the ``${env:...}`` reference that
was authored -- so dumping a validated :class:`~catalog.schema.JobConfig` back
to YAML produces a file that no longer loads. Editing therefore reads the raw
authored mapping (``catalog.writer.find_job``) and writes a raw mapping built
here; a validated model is never the source of what gets written.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic_core import PydanticUndefined

from catalog.schema import (
    AzureBlobSource,
    FtpSource,
    GdriveSource,
    HttpSource,
    LocalSource,
    S3Source,
    SftpSource,
    SmbSource,
    WebdavSource,
)

SECRET_PREFIX = "QI_SECRET_"

_SOURCE_MODELS: dict[str, Any] = {
    "local": LocalSource,
    "s3": S3Source,
    "webdav": WebdavSource,
    "sftp": SftpSource,
    "smb": SmbSource,
    "ftp": FtpSource,
    "gdrive": GdriveSource,
    "azureblob": AzureBlobSource,
    "http": HttpSource,
}

SOURCE_TYPES: tuple[str, ...] = tuple(_SOURCE_MODELS)

MODES: tuple[str, ...] = ("append", "upsert", "full")
CHUNK_STRATEGIES: tuple[str, ...] = ("auto", "markdown", "paragraph", "sheet_rows", "slide")
STARTUP_POLICIES: tuple[str, ...] = ("never", "if_missed", "always")


@dataclass(frozen=True)
class FieldSpec:
    """One rendered input, and how to read it back."""

    key: str
    label: str
    kind: str  # text | integer | bool | secret | list
    required: bool
    default: str = ""


def _kind_for(name: str, annotation: Any, secret_fields: frozenset[str]) -> str:
    if name in secret_fields:
        return "secret"
    if name == "rclone_flags":
        return "list"
    text = str(annotation)
    if "bool" in text:
        return "bool"
    if "int" in text and "str" not in text:
        return "integer"
    return "text"


def _default_for(field: Any) -> str:
    if field.default is PydanticUndefined or field.default is None:
        return ""
    if isinstance(field.default, bool):
        return "true" if field.default else "false"
    return str(field.default)


def _specs_for(model: Any) -> tuple[FieldSpec, ...]:
    secret_fields = model.secret_fields
    specs: list[FieldSpec] = []
    for name, field in model.model_fields.items():
        if name == "type":
            continue
        # `pass` is a Python keyword, so WebdavSource and friends declare the
        # field as `password` with an alias; the file uses the alias.
        key = field.alias or name
        specs.append(
            FieldSpec(
                key=key,
                label=key.replace("_", " "),
                kind=_kind_for(name, field.annotation, secret_fields),
                required=field.is_required(),
                default=_default_for(field),
            )
        )
    return tuple(specs)


SOURCE_FIELDS: dict[str, tuple[FieldSpec, ...]] = {
    name: _specs_for(model) for name, model in _SOURCE_MODELS.items()
}


def available_secret_names(environ: Mapping[str, str]) -> list[str]:
    """The QI_SECRET_* variables that actually carry a value.

    The interface offers these as a choice rather than a free-text field: the
    bundle .env is read-only from in here, so a name that is not already set
    could not be made to work from this side anyway.
    """
    return sorted(
        name
        for name, value in environ.items()
        if name.startswith(SECRET_PREFIX) and value not in (None, "")
    )


def secret_ref(name: str) -> str:
    """The authored form of a secret reference."""
    return f"${{env:{name}}}"


class FormError(ValueError):
    """The submitted form could not be turned into a job mapping."""


def _text(form: Mapping[str, Any], key: str) -> str:
    value = form.get(key)
    return value.strip() if isinstance(value, str) else ""


def _bool(form: Mapping[str, Any], key: str, default: bool = False) -> bool:
    """Read a checkbox, telling "unchecked" apart from "never rendered".

    An unchecked checkbox submits nothing, so absence alone cannot say whether
    the operator cleared the box or the form never carried it. Each checkbox is
    therefore paired with a hidden field of the same name holding the off
    value, and the last value wins -- the standard trick. A key that is missing
    entirely means the field was not part of this form, and the schema default
    stands. Without that distinction a partial form would silently switch
    `safety.empty_source_guard` off.
    """
    getlist = getattr(form, "getlist", None)
    values = getlist(key) if callable(getlist) else ([form[key]] if key in form else [])
    if not values:
        return default
    last = values[-1]
    return str(last).strip().lower() in ("1", "true", "on", "yes")


def _int(form: Mapping[str, Any], key: str) -> int | None:
    raw = _text(form, key)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise FormError(f"{key} must be a whole number") from exc


def _float(form: Mapping[str, Any], key: str) -> float | None:
    raw = _text(form, key)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise FormError(f"{key} must be a number") from exc


def _lines(form: Mapping[str, Any], key: str) -> list[str]:
    """Split a textarea into entries, one per line or comma."""
    raw = _text(form, key)
    if not raw:
        return []
    parts = [part.strip() for chunk in raw.splitlines() for part in chunk.split(",")]
    return [part for part in parts if part]


def _source_from_form(form: Mapping[str, Any]) -> dict[str, Any]:
    source_type = _text(form, "source_type")
    if source_type not in _SOURCE_MODELS:
        raise FormError(f"unknown source type {source_type!r}")

    source: dict[str, Any] = {"type": source_type}
    for spec in SOURCE_FIELDS[source_type]:
        field_name = f"source__{spec.key}"
        if spec.kind == "bool":
            declared_default = spec.default == "true"
            value: Any = _bool(form, field_name, default=declared_default)
            if value == declared_default:
                continue
        elif spec.kind == "integer":
            value = _int(form, field_name)
            if value is None:
                continue
        elif spec.kind == "list":
            value = _lines(form, field_name)
            if not value:
                continue
        elif spec.kind == "secret":
            name = _text(form, field_name)
            if not name:
                continue
            if not name.startswith(SECRET_PREFIX):
                raise FormError(
                    f"{spec.key} must reference a {SECRET_PREFIX}* variable, not a literal"
                )
            value = secret_ref(name)
        else:
            value = _text(form, field_name)
            if not value:
                continue
        source[spec.key] = value
    return source


def job_from_form(form: Mapping[str, Any]) -> dict[str, Any]:
    """Build the raw job mapping a catalog file would carry.

    Only values that were actually supplied are written: a job that keeps
    every default stays three lines long instead of forty, which is what makes
    a hand-edited catalog and a form-edited one look the same.
    """
    job_id = _text(form, "id")
    if not job_id:
        raise FormError("id is required")

    job: dict[str, Any] = {"id": job_id}

    if not _bool(form, "enabled", default=True):
        job["enabled"] = False
    description = _text(form, "description")
    if description:
        job["description"] = description

    job["source"] = _source_from_form(form)

    target: dict[str, Any] = {"collection": _text(form, "target__collection")}
    acl_tags = _lines(form, "target__acl_tags")
    if acl_tags:
        target["acl_tags"] = acl_tags
    extra_payload = _text(form, "target__extra_payload")
    if extra_payload:
        try:
            parsed = json.loads(extra_payload)
        except json.JSONDecodeError as exc:
            raise FormError(f"extra_payload is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise FormError("extra_payload must be a JSON object")
        target["extra_payload"] = parsed
    job["target"] = target

    mode = _text(form, "mode")
    if mode not in MODES:
        raise FormError(f"unknown mode {mode!r}")
    job["mode"] = mode

    full_scope = _text(form, "full_scope")
    if full_scope and full_scope != "job":
        job["full_scope"] = full_scope
    append_probe = _text(form, "append_probe")
    if append_probe and append_probe != "auto":
        job["append_probe"] = append_probe
    if _bool(form, "mcp_allow_full"):
        job["mcp_allow_full"] = True
    if _bool(form, "expand_embedded"):
        job["expand_embedded"] = True

    filters: dict[str, Any] = {}
    include = _lines(form, "filters__include")
    exclude = _lines(form, "filters__exclude")
    max_bytes = _int(form, "filters__max_file_bytes")
    if include:
        filters["include"] = include
    if exclude:
        filters["exclude"] = exclude
    if max_bytes is not None:
        filters["max_file_bytes"] = max_bytes
    if filters:
        job["filters"] = filters

    schedule: dict[str, Any] = {}
    cron = _text(form, "schedule__cron")
    every = _text(form, "schedule__every")
    if cron and every:
        raise FormError("a schedule takes either cron or every, not both")
    if cron:
        schedule["cron"] = cron
    if every:
        schedule["every"] = every
    timezone = _text(form, "schedule__timezone")
    if timezone:
        schedule["timezone"] = timezone
    startup = _text(form, "schedule__run_on_startup")
    if startup and startup != "if_missed":
        schedule["run_on_startup"] = startup
    jitter = _int(form, "schedule__jitter_seconds")
    if jitter is not None and jitter != 30:
        schedule["jitter_seconds"] = jitter
    misfire = _int(form, "schedule__misfire_grace_seconds")
    if misfire is not None and misfire != 300:
        schedule["misfire_grace_seconds"] = misfire
    if schedule:
        job["schedule"] = schedule

    chunking: dict[str, Any] = {}
    strategy = _text(form, "chunking__strategy")
    if strategy and strategy != "auto":
        chunking["strategy"] = strategy
    words = _int(form, "chunking__words")
    if words is not None and words != 400:
        chunking["words"] = words
    overlap = _int(form, "chunking__overlap")
    if overlap is not None and overlap != 50:
        chunking["overlap"] = overlap
    if chunking:
        job["chunking"] = chunking

    embedding: dict[str, Any] = {}
    model = _text(form, "embedding__model")
    if model:
        embedding["model"] = model
    batch_size = _int(form, "embedding__batch_size")
    if batch_size is not None:
        embedding["batch_size"] = batch_size
    if embedding:
        job["embedding"] = embedding

    safety: dict[str, Any] = {}
    ratio = _float(form, "safety__max_delete_ratio")
    if ratio is not None and ratio != 0.25:
        safety["max_delete_ratio"] = ratio
    if not _bool(form, "safety__empty_source_guard", default=True):
        safety["empty_source_guard"] = False
    if safety:
        job["safety"] = safety

    return job


def form_values_from_job(job: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten a raw job mapping into the names the form uses.

    Reads the authored mapping, so a secret field still carries its
    ``${env:...}`` reference and is offered back as the selected variable.
    """
    values: dict[str, Any] = {
        "id": job.get("id", ""),
        "enabled": job.get("enabled", True),
        "description": job.get("description", ""),
        "mode": job.get("mode", "append"),
        "full_scope": job.get("full_scope", "job"),
        "append_probe": job.get("append_probe", "auto"),
        "mcp_allow_full": bool(job.get("mcp_allow_full", False)),
        "expand_embedded": bool(job.get("expand_embedded", False)),
    }

    source = job.get("source") or {}
    values["source_type"] = source.get("type", "local")
    for key, value in source.items():
        if key == "type":
            continue
        if isinstance(value, list):
            values[f"source__{key}"] = "\n".join(str(item) for item in value)
        elif isinstance(value, str) and value.startswith("${env:"):
            values[f"source__{key}"] = value[len("${env:") : -1]
        else:
            values[f"source__{key}"] = value

    target = job.get("target") or {}
    values["target__collection"] = target.get("collection", "")
    values["target__acl_tags"] = "\n".join(target.get("acl_tags") or [])
    extra_payload = target.get("extra_payload") or {}
    values["target__extra_payload"] = (
        json.dumps(extra_payload, indent=2) if extra_payload else ""
    )

    filters = job.get("filters") or {}
    values["filters__include"] = "\n".join(filters.get("include") or [])
    values["filters__exclude"] = "\n".join(filters.get("exclude") or [])
    values["filters__max_file_bytes"] = filters.get("max_file_bytes", "")

    schedule = job.get("schedule") or {}
    values["schedule__cron"] = schedule.get("cron") or ""
    values["schedule__every"] = schedule.get("every") or ""
    values["schedule__timezone"] = schedule.get("timezone") or ""
    values["schedule__run_on_startup"] = schedule.get("run_on_startup", "if_missed")
    values["schedule__jitter_seconds"] = schedule.get("jitter_seconds", 30)
    values["schedule__misfire_grace_seconds"] = schedule.get("misfire_grace_seconds", 300)

    chunking = job.get("chunking") or {}
    values["chunking__strategy"] = chunking.get("strategy", "auto")
    values["chunking__words"] = chunking.get("words", 400)
    values["chunking__overlap"] = chunking.get("overlap", 50)

    embedding = job.get("embedding") or {}
    values["embedding__model"] = embedding.get("model") or ""
    values["embedding__batch_size"] = embedding.get("batch_size", "")

    safety = job.get("safety") or {}
    values["safety__max_delete_ratio"] = safety.get("max_delete_ratio", 0.25)
    values["safety__empty_source_guard"] = safety.get("empty_source_guard", True)

    return values


def blank_form_values() -> dict[str, Any]:
    """Form values for a job that does not exist yet."""
    return form_values_from_job({"id": "", "source": {"type": "local"}, "mode": "append"})
