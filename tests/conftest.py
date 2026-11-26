"""Shared test helpers: recorded portal responses and a fake HTTP layer."""
import json
import os
from pathlib import Path

import pytest

# api.py refuses to start without catalogue credentials (fail fast in production).
# Tests never talk to Keycloak, so dummy values are enough; a real .env still wins.
os.environ.setdefault("KEYCLOAK_CLIENT_SECRET", "test-only")
os.environ.setdefault("KEYCLOAK_PASSWORD", "test-only")

FIXTURES = Path(__file__).parent / "fixtures"


def load_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def load_json(name: str):
    return json.loads(load_text(name))


class FakeResponse:
    """Just enough of requests.Response for the harvesters."""

    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code} error", response=self)


@pytest.fixture(autouse=True)
def isolated_output_dir(tmp_path, monkeypatch):
    """Every harvest replaces jsonld/ and jsonld_records.zip; never let a test touch the real ones."""
    import harvesterv3_Frontend

    monkeypatch.setattr(harvesterv3_Frontend, "OUTPUT_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def no_sleep(monkeypatch):
    """Make retry/back-off waits instant and record how long the code wanted to wait."""
    waits = []
    monkeypatch.setattr("time.sleep", lambda s: waits.append(s))
    return waits
