"""
Protocol harvesters against a fake HTTP layer: paging, record limits, retries
and failures, without touching the real portals.
"""
import pytest
import requests

import protocols.gbif as gbif
import protocols.zenodo as zenodo

from conftest import FakeResponse, load_json, load_text

ZENODO_URL = "https://zenodo.example/api/records"
ZENODO_MAX_RETRIES = 5  # MAX_RETRIES_PER_PAGE inside harvest_zenodo
GBIF_URL = "https://gbif.example/v1/dataset/search"


def zenodo_page(ids, has_next, total=100):
    return FakeResponse(json_data={
        "hits": {"total": total, "hits": [{"id": i} for i in ids]},
        "links": {"next": "more"} if has_next else {},
    })


class Script:
    """Replays a fixed list of responses (or exceptions) and records the requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, params=None, **kwargs):
        self.calls.append((url, dict(params or {})))
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


# ---------------------------------------------------------------- Zenodo

def test_zenodo_follows_pages_until_no_next_link(monkeypatch, no_sleep):
    script = Script(zenodo_page([1, 2], has_next=True), zenodo_page([3], has_next=False))
    monkeypatch.setattr(zenodo.requests, "get", script)

    records = zenodo.harvest_zenodo(ZENODO_URL)

    assert [r["id"] for r in records] == [1, 2, 3]
    assert [params["page"] for _, params in script.calls] == [1, 2]


def test_zenodo_stops_at_max_records(monkeypatch, no_sleep):
    script = Script(zenodo_page([1, 2], has_next=True), zenodo_page([3, 4], has_next=True))
    monkeypatch.setattr(zenodo.requests, "get", script)

    records = zenodo.harvest_zenodo(ZENODO_URL, max_records=3)

    assert [r["id"] for r in records] == [1, 2, 3]


def test_zenodo_sends_query_and_reports_counts(monkeypatch, no_sleep):
    count_call = FakeResponse(json_data={"hits": {"total": 5000}})
    script = Script(count_call, zenodo_page([1], has_next=False, total=42))
    monkeypatch.setattr(zenodo.requests, "get", script)
    stats = {}

    zenodo.harvest_zenodo(ZENODO_URL, clauses=[["wadden"], ["bird", "owl"]], stats=stats)

    assert script.calls[1][1]["q"] == '("wadden") AND ("bird" OR "owl")'
    assert stats["records_found"] == 5000
    assert stats["after_server_side_filtering"] == 42


def test_zenodo_retries_server_errors_and_honours_retry_after(monkeypatch, no_sleep):
    script = Script(FakeResponse(503), FakeResponse(429, headers={"Retry-After": "7"}),
                    zenodo_page([1], has_next=False))
    monkeypatch.setattr(zenodo.requests, "get", script)

    records = zenodo.harvest_zenodo(ZENODO_URL)

    assert [r["id"] for r in records] == [1]
    assert 7 in no_sleep


def test_zenodo_gives_up_on_a_page_but_keeps_what_it_has(monkeypatch, no_sleep):
    failures = [requests.ConnectionError("down")] * (ZENODO_MAX_RETRIES + 1)
    script = Script(zenodo_page([1, 2], has_next=True), *failures)
    monkeypatch.setattr(zenodo.requests, "get", script)

    records = zenodo.harvest_zenodo(ZENODO_URL)

    assert [r["id"] for r in records] == [1, 2]


def test_zenodo_recorded_record_round_trips_through_harvest(monkeypatch, no_sleep):
    record = load_json("zenodo_record.json")
    page = FakeResponse(json_data={"hits": {"total": 1, "hits": [record]}, "links": {}})
    monkeypatch.setattr(zenodo.requests, "get", Script(page))

    assert zenodo.harvest_zenodo(ZENODO_URL) == [record]


# ---------------------------------------------------------------- GBIF

class FakeSession:
    """Routes GBIF search and document requests to canned responses."""

    def __init__(self, search_pages, documents, fail_search_with=None):
        self.search_pages = search_pages      # {q: [response, ...]}
        self.documents = documents            # {key: text or None}
        self.fail_search_with = fail_search_with
        self.searches = []

    def get(self, url, params=None, **kwargs):
        if url == GBIF_URL:
            params = params or {}
            if params.get("limit") == 0:
                return FakeResponse(json_data={"count": 999})
            if self.fail_search_with:
                raise self.fail_search_with
            q = params.get("q", "")
            self.searches.append(q)
            return self.search_pages[q].pop(0)
        key = url.rstrip("/").split("/")[-2]
        text = self.documents.get(key)
        return FakeResponse(200, text=text) if text else FakeResponse(404)


def gbif_hits(*keys, end=True, count=None):
    return FakeResponse(json_data={
        "count": count if count is not None else len(keys),
        "results": [{"key": k, "modified": "2024-05-01T00:00:00Z"} for k in keys],
        "endOfRecords": end,
    })


def use_session(monkeypatch, session):
    monkeypatch.setattr(gbif.requests, "Session", lambda: session)


def test_gbif_or_groups_run_separate_searches_without_duplicates(monkeypatch, no_sleep):
    session = FakeSession(
        search_pages={"bird": [gbif_hits("k1", "k2")], "owl": [gbif_hits("k2", "k3")]},
        documents={"k1": "<eml>1</eml>", "k2": "<eml>2</eml>", "k3": "<eml>3</eml>"},
    )
    use_session(monkeypatch, session)
    stats = {}

    records = gbif.harvest_gbif(GBIF_URL, clauses=[["bird", "owl"]], stats=stats)

    assert session.searches == ["bird", "owl"]
    assert sorted(records) == ["<eml>1</eml>", "<eml>2</eml>", "<eml>3</eml>"]
    assert stats["after_server_side_filtering"] is None  # overlapping searches: no honest total


def test_gbif_date_range_filters_before_downloading(monkeypatch, no_sleep):
    old = FakeResponse(json_data={"count": 2, "endOfRecords": True, "results": [
        {"key": "old", "modified": "2001-01-01T00:00:00Z"},
        {"key": "new", "modified": "2024-05-01T00:00:00Z"},
    ]})
    session = FakeSession(search_pages={"": [old]}, documents={"old": "<eml>old</eml>", "new": "<eml>new</eml>"})
    use_session(monkeypatch, session)

    records = gbif.harvest_gbif(GBIF_URL, start_date="2020-01-01")

    assert records == ["<eml>new</eml>"]


def test_gbif_skips_datasets_whose_document_is_missing(monkeypatch, no_sleep):
    session = FakeSession(search_pages={"": [gbif_hits("ok", "gone")]},
                          documents={"ok": load_text("gbif_eml.xml")})
    use_session(monkeypatch, session)

    records = gbif.harvest_gbif(GBIF_URL)

    assert len(records) == 1 and "Fishes Checklist" in records[0]


def test_gbif_waits_retry_after_on_rate_limit(monkeypatch, no_sleep):
    session = FakeSession(
        search_pages={"": [FakeResponse(429, headers={"Retry-After": "3"}), gbif_hits("k1")]},
        documents={"k1": "<eml/>"},
    )
    use_session(monkeypatch, session)

    assert gbif.harvest_gbif(GBIF_URL) == ["<eml/>"]
    assert 3 in no_sleep


@pytest.mark.xfail(strict=True, raises=requests.ConnectionError,
                   reason="GBIF search has no handling for connection errors (Zenodo retries them)")
def test_gbif_connection_error_does_not_crash_the_harvest(monkeypatch, no_sleep):
    session = FakeSession(search_pages={}, documents={}, fail_search_with=requests.ConnectionError("down"))
    use_session(monkeypatch, session)

    assert gbif.harvest_gbif(GBIF_URL) == []


# ---------------------------------------------------------------- live portals

@pytest.mark.network
@pytest.mark.parametrize("source", ["zenodo", "gbif", "datahuis_wadden", "dans"])
def test_live_portal_returns_mappable_records(source):
    """End-to-end against the real portal. Run with:  pytest -m network"""
    from harvesterv3_Frontend import SOURCES, run_harvest

    cfg = SOURCES[source]
    result = run_harvest(
        portal_url=cfg["display_url"], start_date=None, end_date=None, max_records=2,
        filter_spec={"mode": "basic", "include_terms": [], "exclude_terms": []}, source=source,
    )
    mapped = result.get("mapped_records") or []
    assert mapped, f"{source}: no records mapped"
