"""
How run_harvest hands work to each protocol harvester: arguments, default
record caps, keyword pre-filter and progress messages.

Written before replacing the protocol if/elif chain with a registry, so the
refactor must keep all of this identical.
"""
import pytest

import harvesterv3_Frontend as H

# protocol -> (module, function) of the harvester it must call
HARVESTERS = {
    "OAI-PMH": ("protocols.oai", "harvest_oai"),
    "CSW": ("protocols.csw", "harvest_csw"),
    "GBIF": ("protocols.gbif", "harvest_gbif"),
    "Zenodo": ("protocols.zenodo", "harvest_zenodo"),
    "STAC": ("protocols.stac", "harvest_stac"),
    "GeoNetwork": ("protocols.geonetwork", "harvest_geonetwork"),
    "OData": ("protocols.odata", "harvest_odata"),
}

API = "https://portal.example/api"
FILTER = {"mode": "advanced", "include_terms": [], "exclude_terms": [], "query": "wadden AND (bird OR owl)"}
CLAUSES = [["wadden"], ["bird", "owl"]]


@pytest.fixture
def calls(monkeypatch):
    """Replace every protocol harvester with a recorder that returns no records."""
    recorded = {}
    for proto, (module, func) in HARVESTERS.items():
        def fake(*args, _proto=proto, **kwargs):
            recorded[_proto] = (args, kwargs)
            return []
        monkeypatch.setattr(f"{module}.{func}", fake)
        # Until the refactor the pipeline calls its own imported reference.
        monkeypatch.setattr(H, func, fake, raising=False)
    return recorded


def harvest(monkeypatch, proto, **kwargs):
    monkeypatch.setitem(H.SOURCES, "test_source", {
        "label": "Test", "display_url": "https://portal.example/", "api": API,
        "protocol": proto, "mapper": "dataverse",
    })
    kwargs.setdefault("filter_spec", FILTER)
    return H.run_harvest(portal_url="https://portal.example/", source="test_source", **kwargs)


def test_oai(monkeypatch, calls):
    harvest(monkeypatch, "OAI-PMH", max_records=7, start_date="2020-01-01", end_date="2021-01-01")
    args, kwargs = calls["OAI-PMH"]
    assert args == (API, 7, "2020-01-01", "2021-01-01")
    assert set(kwargs) == {"stats"}


def test_csw(monkeypatch, calls):
    harvest(monkeypatch, "CSW", max_records=7, start_date="2020-01-01",
            filter_spec={"mode": "basic", "include_terms": ["a", "b"], "exclude_terms": ["x"]})
    args, kwargs = calls["CSW"]
    assert args == (API, 7, "2020-01-01", None, ["a", "b"], ["x"])
    assert kwargs["clauses"] == [["a", "b"]]
    assert set(kwargs) == {"stats", "clauses"}


def test_gbif(monkeypatch, calls):
    harvest(monkeypatch, "GBIF", max_records=7)
    args, kwargs = calls["GBIF"]
    assert args == (API, 7, None, None, [], [])
    assert kwargs["clauses"] == CLAUSES
    assert callable(kwargs["progress_cb"])
    assert set(kwargs) == {"progress_cb", "stats", "clauses"}


def test_zenodo(monkeypatch, calls):
    harvest(monkeypatch, "Zenodo", max_records=7)
    args, kwargs = calls["Zenodo"]
    assert args == (API, 7, None, None, [])
    assert kwargs["clauses"] == CLAUSES
    assert set(kwargs) == {"progress_cb", "stats", "clauses"}


@pytest.mark.parametrize("proto", ["STAC", "GeoNetwork", "OData"])
def test_simple_protocols_get_url_and_limit_only(monkeypatch, calls, proto):
    harvest(monkeypatch, proto, max_records=7)
    assert calls[proto] == ((API, 7), {})


@pytest.mark.parametrize("proto, cap", [("Zenodo", 50), ("GBIF", 200), ("STAC", 200), ("OData", 200),
                                        ("OAI-PMH", None), ("CSW", None), ("GeoNetwork", None)])
def test_default_record_cap_without_max_records(monkeypatch, calls, proto, cap):
    result = harvest(monkeypatch, proto)
    assert calls[proto][0][1] == cap
    cap_messages = [m for m in result["progress_messages"] if m.startswith("No record limit given")]
    assert len(cap_messages) == (1 if cap else 0)


@pytest.mark.parametrize("proto, sent", [("CSW", True), ("GBIF", True), ("Zenodo", True),
                                         ("OAI-PMH", False), ("STAC", False)])
def test_prefilter_message_only_for_search_capable_portals(monkeypatch, calls, proto, sent):
    result = harvest(monkeypatch, proto, max_records=1)
    assert any(m.startswith("Keyword pre-filter sent to the portal") for m in result["progress_messages"]) is sent


@pytest.mark.parametrize("proto, begin, total", [
    ("OAI-PMH", "Harvesting records through OAI-PMH.", "Total OAI records harvested: 0"),
    ("CSW", "Harvesting records through CSW (ISO 19139).", "Total CSW records harvested: 0"),
    ("GBIF", "Harvesting records through the GBIF REST API.", "Total GBIF records harvested: 0"),
    ("Zenodo", "Harvesting records through Zenodo REST API.", "Total Zenodo records harvested: 0"),
    ("STAC", "Harvesting records through STAC.", "Total STAC records harvested: 0"),
    ("GeoNetwork", "Harvesting records through GeoNetwork API.", "Total GeoNetwork records harvested: 0"),
    ("OData", "Harvesting records through OData.", "Total OData records harvested: 0"),
])
def test_progress_messages(monkeypatch, calls, proto, begin, total):
    messages = harvest(monkeypatch, proto, max_records=1)["progress_messages"]
    assert begin in messages and total in messages


def test_unknown_protocol_is_reported_not_crashed(monkeypatch, calls):
    result = harvest(monkeypatch, "FTP")
    assert result["progress_messages"] == ["Unsupported protocol."]
    assert calls == {}
