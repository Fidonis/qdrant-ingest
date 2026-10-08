"""The encrypted secret store and the environment layered over it."""

import os
import time
from pathlib import Path

import pytest
import yaml

from api.metrics import Metrics
from catalog import load_catalog, load_catalog_bytes
from catalog.secret_store import LayeredEnviron, SecretStore
from catalog.secrets import SecretResolutionError, resolve_secret
from config import Settings
from connections.crypto import encrypt
from engine import LockingRunner
from engine.service import JobEngine
from sources import build_remote_config

from conftest import AUTH, ApiHarness, EngineHarness
from fakes.connections import FakeConnectionRegistry
from support import make_job, write_catalog, write_connections

KEY = "a-long-random-connections-secret"
OTHER_KEY = "some-other-connections-secret"


def write_secrets(
    path: Path, secrets: dict[str, str], *, key: str = KEY, name: str = "secrets.yaml"
) -> Path:
    """A secrets.yaml with the given plaintext values, encrypted the way the ingester does."""
    document = {
        "version": 1,
        "secrets": [{"name": n, "value": encrypt(v, key)} for n, v in secrets.items()],
    }
    target = path / name
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    return target


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "secrets.yaml"


def test_a_missing_file_is_an_empty_store(store_path: Path) -> None:
    store = SecretStore(store_path, KEY)

    assert store.get("QI_SECRET_X") is None
    assert store.names() == frozenset()
    assert store.problem() is None


def test_stored_values_are_decrypted_on_use(tmp_path: Path, store_path: Path) -> None:
    write_secrets(tmp_path, {"QI_SECRET_S3_KEY": "AKIA123", "QI_SECRET_DAV": "davpass"})
    store = SecretStore(store_path, KEY)

    assert store.get("QI_SECRET_S3_KEY") == "AKIA123"
    assert store.names() == {"QI_SECRET_S3_KEY", "QI_SECRET_DAV"}
    assert store.problem() is None


def test_the_environment_wins_and_the_store_answers_the_rest(
    tmp_path: Path, store_path: Path
) -> None:
    write_secrets(tmp_path, {"QI_SECRET_BOTH": "from-store", "QI_SECRET_ONLY": "stored"})
    environ = LayeredEnviron(
        {"QI_SECRET_BOTH": "from-env", "QI_SECRET_EMPTY": "", "PATH": "/bin"},
        SecretStore(store_path, KEY),
    )

    assert environ["QI_SECRET_BOTH"] == "from-env"
    assert environ["QI_SECRET_ONLY"] == "stored"
    assert environ.get("QI_SECRET_MISSING") is None
    assert "QI_SECRET_MISSING" not in environ
    # An empty variable does not hide a stored value, and stays empty when nothing is stored.
    assert environ["QI_SECRET_EMPTY"] == ""
    assert set(environ) == {"QI_SECRET_BOTH", "QI_SECRET_EMPTY", "PATH", "QI_SECRET_ONLY"}
    assert len(environ) == 4
    assert dict(environ.items())["QI_SECRET_ONLY"] == "stored"


def test_an_empty_variable_is_overridden_by_a_stored_value(
    tmp_path: Path, store_path: Path
) -> None:
    write_secrets(tmp_path, {"QI_SECRET_X": "stored"})
    environ = LayeredEnviron({"QI_SECRET_X": ""}, SecretStore(store_path, KEY))

    assert environ["QI_SECRET_X"] == "stored"


def test_a_change_of_the_file_is_seen_without_a_restart(
    tmp_path: Path, store_path: Path
) -> None:
    store = SecretStore(store_path, KEY)
    assert store.get("QI_SECRET_X") is None

    write_secrets(tmp_path, {"QI_SECRET_X": "first"})
    assert store.get("QI_SECRET_X") == "first"

    # Replaced by renaming a new file over it, which is how the manager writes it.
    staged = write_secrets(tmp_path, {"QI_SECRET_X": "second"}, name=".secrets.tmp")
    os.replace(staged, store_path)
    assert store.get("QI_SECRET_X") == "second"

    store_path.unlink()
    assert store.get("QI_SECRET_X") is None


def test_the_repr_does_not_show_values(tmp_path: Path, store_path: Path) -> None:
    write_secrets(tmp_path, {"QI_SECRET_X": "super-secret-value"})
    environ = LayeredEnviron({}, SecretStore(store_path, KEY))
    environ["QI_SECRET_X"]  # noqa: B018 - read once so a cache would hold it

    assert "super-secret-value" not in repr(environ)


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("jobs: [unterminated", "not valid YAML"),
        ("version: 2\nsecrets: []\n", "version"),
        ("version: 1\nsecrets: []\nextra: 1\n", "extra"),
        ("version: 1\nsecrets:\n  - name: QI_SECRET_X\n    value: plaintext\n", "encrypted"),
        ("version: 1\nsecrets:\n  - name: lower\n    value: enc:1:abc\n", "name"),
        (
            "version: 1\nsecrets:\n"
            "  - {name: QI_SECRET_X, value: 'enc:1:a'}\n"
            "  - {name: QI_SECRET_X, value: 'enc:1:b'}\n",
            "duplicate",
        ),
    ],
)
def test_a_file_that_is_not_a_store_yields_nothing_and_says_why(
    store_path: Path, text: str, fragment: str
) -> None:
    store_path.write_text(text, encoding="utf-8")
    store = SecretStore(store_path, KEY)

    assert store.names() == frozenset()
    problem = store.problem()
    assert problem is not None and fragment in problem
    # Nothing in a broken file can be trusted, so every name is affected.
    assert "unusable" in (store.why_unavailable("QI_SECRET_ANYTHING") or "")


def test_a_rotated_key_makes_the_values_unreadable_and_says_so(
    tmp_path: Path, store_path: Path
) -> None:
    write_secrets(tmp_path, {"QI_SECRET_OLD": "x"}, key=OTHER_KEY)
    store = SecretStore(store_path, KEY)

    assert store.get("QI_SECRET_OLD") is None
    assert "QI_CONNECTIONS_SECRET may have changed" in (store.problem() or "")
    reason = store.why_unavailable("QI_SECRET_OLD") or ""
    assert "cannot be decrypted" in reason and "QI_SECRET_OLD" in reason
    assert store.why_unavailable("QI_SECRET_NEVER_STORED") is None


def test_one_unreadable_value_does_not_take_the_others_with_it(tmp_path: Path) -> None:
    good = write_secrets(tmp_path, {"QI_SECRET_GOOD": "ok"}, name="a.yaml")
    bad = write_secrets(tmp_path, {"QI_SECRET_BAD": "no"}, key=OTHER_KEY, name="b.yaml")
    merged = yaml.safe_load(good.read_text()) | {}
    merged["secrets"] += yaml.safe_load(bad.read_text())["secrets"]
    target = tmp_path / "secrets.yaml"
    target.write_text(yaml.safe_dump(merged), encoding="utf-8")

    store = SecretStore(target, KEY)

    assert store.get("QI_SECRET_GOOD") == "ok"
    assert store.get("QI_SECRET_BAD") is None
    assert store.problem() is not None


def test_without_a_key_the_store_is_unusable_and_the_message_names_the_key(
    tmp_path: Path, store_path: Path
) -> None:
    write_secrets(tmp_path, {"QI_SECRET_X": "x"})
    store = SecretStore(store_path, "")

    assert store.get("QI_SECRET_X") is None
    assert "QI_CONNECTIONS_SECRET is not set" in (store.problem() or "")


# -- the loader and the sync see the same values ------------------------------------


def _dav_job(secret: str = "QI_SECRET_DAV") -> dict[str, object]:
    return make_job(
        id="dav-job",
        source={
            "type": "webdav",
            "label": "cloud",
            "url": "https://cloud.test/dav",
            "user": "svc",
            "pass": f"${{env:{secret}}}",
        },
    )


def _settings(tmp_path: Path) -> Settings:
    return Settings(local_dir=str(tmp_path / "local"))


def test_a_job_validates_against_a_stored_secret(tmp_path: Path, store_path: Path) -> None:
    write_secrets(tmp_path, {"QI_SECRET_DAV": "davpass"})
    environ = LayeredEnviron({}, SecretStore(store_path, KEY))
    catalog = write_catalog(tmp_path, _dav_job())

    result = load_catalog(catalog, _settings(tmp_path), environ)

    assert result.ok, result.errors


def test_a_missing_secret_keeps_the_plain_message(tmp_path: Path, store_path: Path) -> None:
    environ = LayeredEnviron({}, SecretStore(store_path, KEY))
    catalog = write_catalog(tmp_path, _dav_job())

    result = load_catalog(catalog, _settings(tmp_path), environ)

    assert [issue.message for issue in result.errors] == [
        "referenced environment variable 'QI_SECRET_DAV' is not set"
    ]


def test_an_unreadable_secret_is_reported_as_unreadable_not_as_missing(
    tmp_path: Path, store_path: Path
) -> None:
    write_secrets(tmp_path, {"QI_SECRET_DAV": "davpass"}, key=OTHER_KEY)
    environ = LayeredEnviron({}, SecretStore(store_path, KEY))
    catalog = write_catalog(tmp_path, _dav_job())

    result = load_catalog(catalog, _settings(tmp_path), environ)

    assert len(result.errors) == 1
    assert "cannot be decrypted" in result.errors[0].message
    assert result.errors[0].field == "source.password"


def test_validating_candidate_text_sees_the_store_too(tmp_path: Path, store_path: Path) -> None:
    write_secrets(tmp_path, {"QI_SECRET_DAV": "davpass"})
    environ = LayeredEnviron({}, SecretStore(store_path, KEY))
    raw = write_catalog(tmp_path, _dav_job()).read_text(encoding="utf-8")

    assert load_catalog_bytes(raw, _settings(tmp_path), environ).ok


def test_the_rclone_configuration_is_built_from_the_store(
    tmp_path: Path, store_path: Path
) -> None:
    from catalog.schema import JobConfig

    write_secrets(tmp_path, {"QI_SECRET_DAV": "davpass"})
    environ = LayeredEnviron({}, SecretStore(store_path, KEY))
    job = JobConfig.model_validate(_dav_job())

    conf, _remote = build_remote_config(job.source, environ, lambda value: f"OBSCURED({value})")

    assert "pass = OBSCURED(davpass)" in conf


def test_resolving_an_unreadable_secret_raises_with_the_reason(
    tmp_path: Path, store_path: Path
) -> None:
    write_secrets(tmp_path, {"QI_SECRET_DAV": "x"}, key=OTHER_KEY)
    environ = LayeredEnviron({}, SecretStore(store_path, KEY))

    with pytest.raises(SecretResolutionError, match="cannot be decrypted"):
        resolve_secret("QI_SECRET_DAV", environ)
    with pytest.raises(SecretResolutionError, match="is not set"):
        resolve_secret("QI_SECRET_NOPE", environ)


# -- the engine ------------------------------------------------------------------


def test_the_health_endpoint_announces_the_store_and_degrades_with_it(
    api: ApiHarness, tmp_path: Path
) -> None:
    store_file = write_secrets(tmp_path, {"QI_SECRET_X": "x"}, key=OTHER_KEY)
    api.engine._secret_store = SecretStore(store_file, KEY)  # noqa: SLF001

    body = api.client.get("/health").json()

    assert "secret_store" in body["features"]
    assert body["status"] == "degraded"
    assert body["config_error"].startswith("secrets.yaml:")
    assert api.client.get("/health", headers=AUTH).status_code == 200  # still serving


def test_a_secret_that_appears_makes_a_waiting_job_valid_without_a_restart(
    engine: EngineHarness, tmp_path: Path
) -> None:
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    jobs_path = catalog_dir / "jobs.yaml"
    jobs_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "defaults": {"embedding": {"model": "test-model"}},
                "jobs": [_dav_job()],
            }
        ),
        encoding="utf-8",
    )
    secrets_path = catalog_dir / "secrets.yaml"
    settings = engine.settings.model_copy(
        update={
            "jobs_file": str(jobs_path),
            "secrets_file": str(secrets_path),
            "connections_secret": KEY,
            "jobs_reload_interval": 1,
        }
    )
    registry = FakeConnectionRegistry(settings, engine.writer)
    registry.reload(initial=True)
    store = SecretStore(secrets_path, KEY)
    job_engine = JobEngine(
        settings,
        engine.state,
        registry,
        LockingRunner(engine.runner, engine.state, lock_timeout=5.0),
        environ=LayeredEnviron({}, store),
        secret_store=store,
        metrics_hook=Metrics().record_run,
    )
    try:
        job_engine.startup(fire_startup_runs=False)
        assert job_engine.jobs() == []  # at start the valid subset loads: nothing

        write_secrets(catalog_dir, {"QI_SECRET_DAV": "davpass"})

        deadline = time.monotonic() + 10
        while not job_engine.jobs() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert [job.id for job in job_engine.jobs()] == ["dav-job"]
        assert job_engine.config_info()["valid"] is True
    finally:
        job_engine.shutdown()


def test_the_assembled_service_syncs_with_the_stored_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main.build_engine hands the layered environment to the sync, not just the loader.

    Without that a stored secret would validate and then fail at the first run, because the
    sync would look in the bare process environment.
    """
    import main
    from sources.rclone import SyncResult

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    write_secrets(catalog_dir, {"QI_SECRET_DAV": "davpass"})
    write_connections(catalog_dir)
    jobs_path = write_catalog(catalog_dir, _dav_job())
    seen: list[str] = []

    def spy(job: object, settings: object, environ: object = None, **_kwargs: object) -> SyncResult:
        assert environ is not None
        seen.append(resolve_secret("QI_SECRET_DAV", environ))  # type: ignore[arg-type]
        return SyncResult(ok=False, returncode=1, stderr_tail="", duration_seconds=0.0)

    monkeypatch.setattr(main, "sync_job", spy)
    settings = Settings(
        state_dir=str(tmp_path / "state"),
        cache_dir=str(tmp_path / "cache"),
        local_dir=str(tmp_path / "local"),
        jobs_file=str(jobs_path),
        connections_file=str(catalog_dir / "connections.yaml"),
        secrets_file=str(catalog_dir / "secrets.yaml"),
        connections_secret=KEY,
    )
    job_engine = main.build_engine(settings, Metrics())
    try:
        job_engine.reload_connections(initial=True)
        job_engine.reload_config(initial=True)
        assert [job.id for job in job_engine.jobs()] == ["dav-job"]

        run = job_engine.run_sync("dav-job", "manual_rest")

        assert seen == ["davpass"]
        assert run.status == "failed"  # the spy refused the sync; what matters is the value
        assert job_engine.environ["QI_SECRET_DAV"] == "davpass"
        assert "secret_store" in job_engine.health()["features"]
    finally:
        job_engine.shutdown()
