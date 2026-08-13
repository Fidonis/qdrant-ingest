"""The auth-free health endpoint."""

from fastapi.testclient import TestClient

from config import APP_VERSION, Settings
from main import create_app


def test_health_ok() -> None:
    client = TestClient(create_app(Settings()))
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == APP_VERSION
