import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.version import APP_VERSION
from app.main import app

client = TestClient(app)


@pytest.mark.parametrize("demo", [True, False])
def test_health_returns_ok_and_whether_the_demo_is_on(monkeypatch: pytest.MonkeyPatch, demo: bool) -> None:
    monkeypatch.setattr(settings, "demo_mode_enabled", demo)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": APP_VERSION, "demo_mode": demo}


def test_version_is_kept_in_one_place() -> None:
    assert app.version == APP_VERSION
    assert app.openapi()["info"]["version"] == APP_VERSION
