"""
Web API: the harvest job lifecycle (start -> poll -> done/error) with the
pipeline replaced by a stub, so no portal is contacted.
"""
import pytest
from fastapi.testclient import TestClient

import api


class InlineThread:
    """Runs the background job immediately, so a test can poll the final state."""

    def __init__(self, target, kwargs=None, **_):
        self.target, self.kwargs = target, kwargs or {}

    def start(self):
        self.target(**self.kwargs)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api.threading, "Thread", InlineThread)
    return TestClient(api.app)


def start_job(client, **form):
    data = {"node_name": "test-node", "url": "https://zenodo.org/", "source": "zenodo", **form}
    response = client.post("/run", data=data)
    assert response.status_code == 200
    return response.json()["job_id"]


def test_form_page_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="filter_query"' in response.text


def test_unknown_job_is_404(client):
    assert client.get("/run-status/does-not-exist").status_code == 404


def test_successful_job_reports_records_and_passes_the_filter_through(client, monkeypatch):
    seen = {}

    def fake_run_harvest(**kwargs):
        seen.update(kwargs)
        return {
            "ui_records": [{"Title": "a"}, {"separator": True}, {"Title": "b"}],
            "mapped_records": [{"Title": {"value": "a"}}, {"Title": {"value": "b"}}],
            "filter_info": {"mode": "advanced"},
            "progress_messages": ["Harvested 2 records."],
            "harvest_report": {"records_harvested": 2},
        }

    monkeypatch.setattr(api, "run_harvest", fake_run_harvest)

    job_id = start_job(client, filter_mode="advanced", filter_query="wadden AND bird", max_records="5")
    status = client.get(f"/run-status/{job_id}").json()

    assert status["status"] == "done", status["error"]
    assert status["result"]["record_count"] == 2
    assert status["result"]["harvest_report"]["source"] == "Zenodo"
    assert seen["filter_spec"]["query"] == "wadden AND bird"
    assert seen["max_records"] == 5
    assert seen["run_llm"] is False


@pytest.mark.parametrize("raw, expected", [("", None), ("0", None), ("-3", None), ("abc", None), (" 12 ", 12)])
def test_max_records_is_parsed_leniently(client, monkeypatch, raw, expected):
    seen = {}
    monkeypatch.setattr(api, "run_harvest", lambda **kw: seen.update(kw) or {})
    start_job(client, max_records=raw)
    assert seen["max_records"] == expected


def test_pipeline_exception_marks_job_as_error(client, monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("portal exploded")

    monkeypatch.setattr(api, "run_harvest", broken)

    status = client.get(f"/run-status/{start_job(client)}").json()

    assert status["status"] == "error"
    assert "portal exploded" in status["error"]


def test_unsupported_url_is_rejected_without_harvesting(client, monkeypatch):
    monkeypatch.setattr(api, "run_harvest", lambda **kw: pytest.fail("must not harvest"))

    job_id = start_job(client, url="https://unknown-portal.example/", source="")
    status = client.get(f"/run-status/{job_id}").json()

    assert status["status"] == "error"
    assert "schema mapping is not performed" in status["error"]
