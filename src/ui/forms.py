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
import re
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

# Offered as a datalist next to the schedule timezone field. Free text is still
# accepted -- this is a shortcut, not a whitelist.
COMMON_TIMEZONES: tuple[str, ...] = (
    "UTC",
    "Europe/Berlin",
    "Europe/London",
    "Europe/Paris",
    "Europe/Madrid",
    "Europe/Rome",
    "Europe/Warsaw",
    "Europe/Athens",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Singapore",
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Australia/Sydney",
)


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
    connection = _text(form, "target__connection")
    if connection:
        target["connection"] = connection
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


_EVERY_FORM_RE = re.compile(r"^(\d+)(s|m|h|d)$")
_CRON_INT_RE = re.compile(r"^\d+$")

# The schedule builder in job_edit.html emits a small, fixed set of cron shapes.
# These are the defaults it opens on; a recognised schedule overrides the ones
# it implies.
_SCHEDULE_UI_DEFAULTS: dict[str, Any] = {
    "schedule__ui_mode": "manual",
    "schedule__ui_freq": "daily",
    "schedule__ui_time": "03:00",
    "schedule__ui_minute": 0,
    "schedule__ui_dom": 1,
    "schedule__ui_every_n": 15,
    "schedule__ui_every_unit": "m",
}


def _cron_int(token: str, low: int, high: int) -> int | None:
    """A plain integer cron token inside ``[low, high]``, or ``None``.

    Ranges, steps and lists all return ``None`` -- those shapes belong in the
    raw expression field, not a builder control.
    """
    if _CRON_INT_RE.match(token) is None:
        return None
    value = int(token)
    return value if low <= value <= high else None


def classify_schedule(cron: str | None, every: str | None) -> dict[str, Any]:
    """Map an authored schedule onto the job form's builder controls.

    The builder writes a daily time, an hourly minute, a weekday list, or a day
    of the month -- plus ``every`` intervals. A schedule that matches one of
    those re-opens on that control; anything else (ranges, steps, several hours)
    is handed to the raw "cron expression" field so nothing is silently lost.
    Day-of-week follows APScheduler's ``from_crontab``: ``0`` is Monday,
    ``6`` is Sunday.
    """
    ui: dict[str, Any] = {**_SCHEDULE_UI_DEFAULTS, "schedule__ui_weekdays": []}
    cron = (cron or "").strip()
    every = (every or "").strip()

    if every:
        match = _EVERY_FORM_RE.match(every)
        if match is None:
            ui["schedule__ui_mode"] = "cron"
        else:
            ui["schedule__ui_mode"] = "interval"
            ui["schedule__ui_every_n"] = int(match.group(1))
            ui["schedule__ui_every_unit"] = match.group(2)
        return ui

    if not cron:
        return ui

    ui["schedule__ui_mode"] = "cron"
    fields = cron.split()
    if len(fields) != 5:
        return ui
    minute, hour, dom, month, dow = fields
    minute_val = _cron_int(minute, 0, 59)

    if minute_val is not None and hour == "*" and dom == "*" and month == "*" and dow == "*":
        ui["schedule__ui_mode"] = "recurring"
        ui["schedule__ui_freq"] = "hourly"
        ui["schedule__ui_minute"] = minute_val
        return ui

    hour_val = _cron_int(hour, 0, 23)
    if minute_val is None or hour_val is None or month != "*":
        return ui
    ui["schedule__ui_time"] = f"{hour_val:02d}:{minute_val:02d}"

    if dom == "*" and dow == "*":
        ui["schedule__ui_mode"] = "recurring"
        ui["schedule__ui_freq"] = "daily"
        return ui

    if dom == "*" and dow != "*":
        days: list[int] = []
        for part in dow.split(","):
            day = _cron_int(part, 0, 6)
            if day is None:
                return ui
            days.append(day)
        ui["schedule__ui_mode"] = "recurring"
        ui["schedule__ui_freq"] = "weekly"
        ui["schedule__ui_weekdays"] = sorted(set(days))
        return ui

    if dow == "*":
        day_of_month = _cron_int(dom, 1, 31)
        if day_of_month is not None:
            ui["schedule__ui_mode"] = "recurring"
            ui["schedule__ui_freq"] = "monthly"
            ui["schedule__ui_dom"] = day_of_month
            return ui

    return ui


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
    values["target__connection"] = target.get("connection", "")
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
    values.update(classify_schedule(values["schedule__cron"], values["schedule__every"]))

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


# -- connections --------------------------------------------------------------


def connection_from_form(form: Mapping[str, Any]) -> dict[str, Any]:
    """Build the raw connection mapping from the edit form.

    ``api_key`` is returned as the plaintext the operator typed, if any -- the
    route encrypts it (or reuses the stored token when the field was left
    blank on an edit).
    """
    name = _text(form, "name")
    if not name:
        raise FormError("name is required")
    url = _text(form, "url")
    if not url:
        raise FormError("url is required")

    mapping: dict[str, Any] = {"name": name, "url": url}
    api_key = _text(form, "api_key")
    if api_key:
        mapping["api_key"] = api_key
    return mapping


def connection_form_values(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten a raw connection mapping for the edit form.

    The stored ``enc:1:`` token is never sent to the browser; the form only
    learns whether a key is set.
    """
    return {
        "name": raw.get("name", ""),
        "url": raw.get("url", ""),
        "has_key": bool(raw.get("api_key")),
    }


def blank_connection_values() -> dict[str, Any]:
    return {"name": "", "url": "", "has_key": False}
