"""Form to job mapping.

The recurring hazard here is the secret reference. After validation a
``SecretRef`` field holds the *environment variable name*, not the authored
``${env:...}`` string -- so anything that round-trips a validated model back
into YAML produces a file that no longer loads. These tests pin the authored
form at both ends.
"""

import pytest

from catalog.schema import JobConfig
from ui import forms


def _base_form(**overrides: str) -> dict[str, str]:
    form = {
        "id": "docs",
        "enabled": "1",
        "source_type": "local",
        "source__label": "docs",
        "source__path": "/data/local/docs",
        "target__collection": "col-a",
        "mode": "append",
    }
    form.update(overrides)
    return form


# -- field specs are derived, not typed out ---------------------------------


def test_every_source_type_has_a_spec() -> None:
    assert set(forms.SOURCE_FIELDS) == set(forms.SOURCE_TYPES)


def test_specs_use_the_alias_the_file_actually_carries() -> None:
    """`pass` is a keyword, so the model calls it `password` and aliases it."""
    keys = [spec.key for spec in forms.SOURCE_FIELDS["webdav"]]
    assert "pass" in keys
    assert "password" not in keys


def test_secret_fields_are_recognised_as_secret() -> None:
    kinds = {spec.key: spec.kind for spec in forms.SOURCE_FIELDS["s3"]}
    assert kinds["access_key_id"] == "secret"
    assert kinds["secret_access_key"] == "secret"
    assert kinds["bucket"] == "text"


def test_ports_are_numeric_and_flags_are_boolean() -> None:
    kinds = {spec.key: spec.kind for spec in forms.SOURCE_FIELDS["ftp"]}
    assert kinds["port"] == "integer"
    assert kinds["tls"] == "bool"


def test_required_fields_are_marked() -> None:
    required = {spec.key for spec in forms.SOURCE_FIELDS["s3"] if spec.required}
    assert "bucket" in required
    assert "label" in required
    assert "region" not in required


# -- form to job ------------------------------------------------------------


def test_a_minimal_form_produces_a_minimal_job() -> None:
    job = forms.job_from_form(_base_form())
    assert job == {
        "id": "docs",
        "source": {"type": "local", "label": "docs", "path": "/data/local/docs"},
        "target": {"collection": "col-a"},
        "mode": "append",
    }


def test_the_result_validates_against_the_schema() -> None:
    JobConfig.model_validate(forms.job_from_form(_base_form()))


def test_an_unchecked_box_disables_the_job() -> None:
    job = forms.job_from_form(_base_form(enabled=""))
    assert job["enabled"] is False


def test_lists_accept_lines_and_commas() -> None:
    job = forms.job_from_form(
        _base_form(target__acl_tags="team:qa\nteam:ops, team:legal")
    )
    assert job["target"]["acl_tags"] == ["team:qa", "team:ops", "team:legal"]


def test_a_secret_choice_becomes_an_authored_reference() -> None:
    job = forms.job_from_form(
        {
            "id": "dav",
            "enabled": "1",
            "source_type": "webdav",
            "source__label": "dav",
            "source__url": "https://dav.test",
            "source__pass": "QI_SECRET_WEBDAV",
            "target__collection": "col-a",
            "mode": "append",
        }
    )
    assert job["source"]["pass"] == "${env:QI_SECRET_WEBDAV}"
    # And it survives validation, which is the whole point.
    JobConfig.model_validate(job)


def test_a_literal_credential_is_refused_before_it_reaches_the_file() -> None:
    with pytest.raises(forms.FormError) as excinfo:
        forms.job_from_form(
            {
                "id": "dav",
                "enabled": "1",
                "source_type": "webdav",
                "source__label": "dav",
                "source__url": "https://dav.test",
                "source__pass": "hunter2",
                "target__collection": "col-a",
                "mode": "append",
            }
        )
    assert "not a literal" in str(excinfo.value)


def test_an_empty_secret_choice_is_simply_absent() -> None:
    job = forms.job_from_form(
        {
            "id": "dav",
            "enabled": "1",
            "source_type": "webdav",
            "source__label": "dav",
            "source__url": "https://dav.test",
            "source__pass": "",
            "target__collection": "col-a",
            "mode": "append",
        }
    )
    assert "pass" not in job["source"]


def test_cron_and_every_together_are_refused() -> None:
    with pytest.raises(forms.FormError):
        forms.job_from_form(_base_form(schedule__cron="0 3 * * *", schedule__every="15m"))


def test_a_non_numeric_number_is_named_in_the_error() -> None:
    with pytest.raises(forms.FormError) as excinfo:
        forms.job_from_form(_base_form(chunking__words="soon"))
    assert "chunking__words" in str(excinfo.value)


def test_extra_payload_must_be_a_json_object() -> None:
    with pytest.raises(forms.FormError):
        forms.job_from_form(_base_form(target__extra_payload="[1, 2]"))

    job = forms.job_from_form(_base_form(target__extra_payload='{"origin": "test"}'))
    assert job["target"]["extra_payload"] == {"origin": "test"}


def test_an_unknown_mode_is_refused() -> None:
    with pytest.raises(forms.FormError):
        forms.job_from_form(_base_form(mode="sideways"))


def test_an_unknown_source_type_is_refused() -> None:
    with pytest.raises(forms.FormError):
        forms.job_from_form(_base_form(source_type="carrier-pigeon"))


# -- job to form ------------------------------------------------------------


def test_form_values_expose_the_variable_name_not_the_reference() -> None:
    """The select shows QI_SECRET_WEBDAV; the file keeps ${env:...}."""
    values = forms.form_values_from_job(
        {
            "id": "dav",
            "source": {"type": "webdav", "label": "dav", "pass": "${env:QI_SECRET_WEBDAV}"},
            "target": {"collection": "col-a"},
            "mode": "append",
        }
    )
    assert values["source__pass"] == "QI_SECRET_WEBDAV"


def test_a_job_survives_a_round_trip_through_the_form() -> None:
    original = {
        "id": "docs",
        "description": "Team documents",
        "source": {"type": "local", "label": "docs", "path": "/data/local/docs"},
        "target": {"collection": "col-a", "acl_tags": ["team:qa"]},
        "mode": "upsert",
    }
    values = forms.form_values_from_job(original)
    # What the browser would post back: every value as a string.
    posted = {key: ("1" if value is True else str(value)) for key, value in values.items()}
    posted = {key: value for key, value in posted.items() if value not in ("", "None", "False")}

    assert forms.job_from_form(posted) == original


def test_blank_values_render_a_usable_empty_form() -> None:
    values = forms.blank_form_values()
    assert values["id"] == ""
    assert values["source_type"] == "local"
    assert values["enabled"] is True


def test_available_secret_names_lists_only_populated_slots() -> None:
    names = forms.available_secret_names(
        {
            "QI_SECRET_ONE": "value",
            "QI_SECRET_EMPTY": "",
            "QI_QDRANT_API_KEY": "not-offered",
            "PATH": "/usr/bin",
        }
    )
    assert names == ["QI_SECRET_ONE"]


# -- checkbox semantics -----------------------------------------------------
#
# An unchecked checkbox submits nothing, so "absent" and "off" look alike in a
# naive reader. They must not: a form that never rendered the safety section
# would otherwise switch empty_source_guard off on every save.


class _MultiForm(dict):
    """Stands in for Starlette FormData, which keeps repeated keys."""

    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        super().__init__(pairs)
        self._pairs = pairs

    def getlist(self, key: str) -> list[str]:
        return [value for name, value in self._pairs if name == key]


def test_a_field_absent_from_the_form_keeps_its_schema_default() -> None:
    job = forms.job_from_form(_base_form())
    assert "safety" not in job
    assert "enabled" not in job


def test_an_unchecked_box_is_written_as_off() -> None:
    form = _MultiForm(
        [
            ("id", "docs"),
            ("source_type", "local"),
            ("source__label", "docs"),
            ("source__path", "/data/local/docs"),
            ("target__collection", "col-a"),
            ("mode", "append"),
            ("enabled", "0"),
            ("safety__empty_source_guard", "0"),
        ]
    )
    job = forms.job_from_form(form)
    assert job["enabled"] is False
    assert job["safety"] == {"empty_source_guard": False}


def test_a_checked_box_wins_over_its_hidden_partner() -> None:
    form = _MultiForm(
        [
            ("id", "docs"),
            ("source_type", "local"),
            ("source__label", "docs"),
            ("source__path", "/data/local/docs"),
            ("target__collection", "col-a"),
            ("mode", "append"),
            ("enabled", "0"),
            ("enabled", "1"),
            ("safety__empty_source_guard", "0"),
            ("safety__empty_source_guard", "1"),
        ]
    )
    job = forms.job_from_form(form)
    assert "enabled" not in job
    assert "safety" not in job


def test_a_source_flag_at_its_default_is_not_written() -> None:
    form = _MultiForm(
        [
            ("id", "files"),
            ("source_type", "ftp"),
            ("source__label", "files"),
            ("source__host", "ftp.test"),
            ("source__tls", "0"),
            ("target__collection", "col-a"),
            ("mode", "append"),
        ]
    )
    assert "tls" not in forms.job_from_form(form)["source"]


def test_a_source_flag_turned_on_is_written() -> None:
    form = _MultiForm(
        [
            ("id", "files"),
            ("source_type", "ftp"),
            ("source__label", "files"),
            ("source__host", "ftp.test"),
            ("source__tls", "0"),
            ("source__tls", "1"),
            ("target__collection", "col-a"),
            ("mode", "append"),
        ]
    )
    assert forms.job_from_form(form)["source"]["tls"] is True
