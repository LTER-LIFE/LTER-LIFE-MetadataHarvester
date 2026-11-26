"""
Protocol registry: everything the pipeline needs to know about a repository
interface, in one place.

Each protocol module keeps its own harvest function and signature. A small
adapter here translates the pipeline's HarvestRequest into that call, so
run_harvest() no longer needs one if/elif branch per protocol, and adding a
protocol means adding one entry to PROTOCOLS.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from protocols import csw, gbif, geonetwork, oai, odata, stac, zenodo


@dataclass
class HarvestRequest:
    """What the pipeline asks of a harvester. Adapters pass on what their protocol supports."""

    api: str
    limit: Optional[int] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    include_terms: list[str] = field(default_factory=list)
    exclude_terms: list[str] = field(default_factory=list)
    clauses: list[list[str]] = field(default_factory=list)   # server-side keyword pre-filter
    progress_cb: Optional[Callable[[str], None]] = None
    stats: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Protocol:
    name: str                        # value used in SOURCES / KNOWN_APIS, e.g. "OAI-PMH"
    harvest: Callable[[HarvestRequest], list[Any]]
    description: str                 # "Harvesting records through {description}."
    record_label: str                # "Total {record_label} records harvested: n"
    keyword_prefilter: bool = False  # portal can pre-filter on keywords server-side
    default_cap: Optional[int] = None  # record limit when the user sets none (REST APIs never end)


# Adapters look the harvest function up on the module at call time, so tests
# can replace e.g. protocols.oai.harvest_oai.

def _oai(r: HarvestRequest):
    return oai.harvest_oai(r.api, r.limit, r.start_date, r.end_date, stats=r.stats)


def _csw(r: HarvestRequest):
    return csw.harvest_csw(r.api, r.limit, r.start_date, r.end_date, r.include_terms, r.exclude_terms,
                           stats=r.stats, clauses=r.clauses)


def _gbif(r: HarvestRequest):
    return gbif.harvest_gbif(r.api, r.limit, r.start_date, r.end_date, r.include_terms, r.exclude_terms,
                             progress_cb=r.progress_cb, stats=r.stats, clauses=r.clauses)


def _zenodo(r: HarvestRequest):
    return zenodo.harvest_zenodo(r.api, r.limit, r.start_date, r.end_date, r.include_terms,
                                 progress_cb=r.progress_cb, stats=r.stats, clauses=r.clauses)


def _stac(r: HarvestRequest):
    return stac.harvest_stac(r.api, r.limit)


def _geonetwork(r: HarvestRequest):
    return geonetwork.harvest_geonetwork(r.api, r.limit)


def _odata(r: HarvestRequest):
    return odata.harvest_odata(r.api, r.limit)


PROTOCOLS: dict[str, Protocol] = {p.name: p for p in [
    Protocol("OAI-PMH", _oai, "OAI-PMH", "OAI"),
    Protocol("CSW", _csw, "CSW (ISO 19139)", "CSW", keyword_prefilter=True),
    Protocol("GBIF", _gbif, "the GBIF REST API", "GBIF", keyword_prefilter=True, default_cap=200),
    Protocol("Zenodo", _zenodo, "Zenodo REST API", "Zenodo", keyword_prefilter=True, default_cap=50),
    Protocol("STAC", _stac, "STAC", "STAC", default_cap=200),
    Protocol("GeoNetwork", _geonetwork, "GeoNetwork API", "GeoNetwork"),
    Protocol("OData", _odata, "OData", "OData", default_cap=200),
]}


def get_protocol(name: str) -> Optional[Protocol]:
    return PROTOCOLS.get(name)
