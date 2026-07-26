import pytest
from fastapi.testclient import TestClient

from heydey.server import create_app

TEST_TOKEN = "test-token-s0-fixed"


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture()
def heydey_home(tmp_path, monkeypatch):
    """Point HEYDEY_HOME at a throwaway dir so tests never touch ~/.heydey."""
    home = tmp_path / "heydey-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HEYDEY_HOME", str(home))
    return home


@pytest.fixture()
def client(heydey_home):
    app = create_app(TEST_TOKEN)
    with TestClient(app) as test_client:
        yield test_client
