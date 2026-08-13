"""Secret references for the job catalog.

No credential value ever lives in ``jobs.yaml``. Every secret-typed field
accepts exactly the form ``${env:QI_SECRET_<NAME>}`` — a literal in such a
field is a hard validation error, which makes the file commit-safe by
construction rather than by convention.

Only names matching ``QI_SECRET_[A-Z0-9_]+`` may be referenced. A manipulated
``jobs.yaml`` therefore cannot exfiltrate other process variables such as
``QI_QDRANT_API_KEY`` or ``QI_API_TOKEN``.
"""

import os
import re
from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import BeforeValidator

_REF_RE = re.compile(r"^\$\{env:([A-Za-z0-9_]+)\}$")
_ALLOWED_NAME_RE = re.compile(r"^QI_SECRET_[A-Z0-9_]+$")


class SecretResolutionError(Exception):
    """A referenced secret variable is missing from the environment."""


def parse_secret_ref(value: Any) -> str:
    """Validate a ``${env:...}`` reference and return the environment name."""
    if not isinstance(value, str):
        raise ValueError("secret fields accept only a ${env:QI_SECRET_<NAME>} reference")
    match = _REF_RE.match(value.strip())
    if match is None:
        raise ValueError(
            "literal credential values are not allowed in jobs.yaml; "
            "use ${env:QI_SECRET_<NAME>} and put the value into the environment"
        )
    name = match.group(1)
    if _ALLOWED_NAME_RE.match(name) is None:
        raise ValueError(
            f"'{name}' is not resolvable from jobs.yaml; "
            "only QI_SECRET_<NAME> variables may be referenced"
        )
    return name


# After validation the field holds the *environment variable name*, never the
# secret value itself. Resolution happens on use, via resolve_secret().
SecretRef = Annotated[str, BeforeValidator(parse_secret_ref)]


def resolve_secret(name: str, environ: Mapping[str, str] | None = None) -> str:
    """Return the secret value for a validated reference name."""
    env = os.environ if environ is None else environ
    value = env.get(name)
    if value is None or value == "":
        raise SecretResolutionError(f"environment variable '{name}' is not set")
    return value
