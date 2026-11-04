from pathlib import Path

from fastapi.testclient import TestClient

from app.api.main import create_app
from app.bootstrap import build
from app.settings import Settings


def test_health(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 's.db'}")
    with TestClient(create_app(build(settings))) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_favicon_is_served_without_a_login(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 's.db'}")
    with TestClient(create_app(build(settings))) as client:
        response = client.get("/favicon.ico")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/x-icon"
    assert response.content.startswith(b"\x00\x00\x01\x00")  # the ICO file signature
