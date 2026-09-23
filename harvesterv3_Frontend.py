import os
import re
import requests
import xml.etree.ElementTree as ET
import json
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlparse
from typing import Callable, Optional

from protocols.oai import harvest_oai
from protocols.csw import harvest_csw
from protocols.stac import harvest_stac
from protocols.geonetwork import harvest_geonetwork
from protocols.odata import harvest_odata
from protocols.gbif import harvest_gbif

from converters.lterlife_mapper import map_record_to_lterlife
from protocols.zenodo import harvest_zenodo
from converters.zenodo_mapper import map_zenodo_record_to_lterlife
from converters.gbif_mapper import map_gbif_record_to_lterlife
from converters.rivm_mapper import map_rivm_record_to_lterlife
from converters.dans_mapper import map_dans_record_to_lterlife
from converters.iso19139_mapper import map_iso19139_record_to_lterlife
from exporters.jsonld_exporter import export_jsonld_records, JSONLD_CONTEXT
from exporters.zip_utils import create_zip
from llm_harvester_client import enrich_url

requests.packages.urllib3.disable_warnings()

# =====================================================
# LLM PROVENANCE TAGGING
# =====================================================
# Single source of truth for the tag written into a field's "raw_fields"
# whenever that field's value was produced/filled by the LLM harvester
# service rather than the original metadata source.
LLM_SOURCE_TAG = "LLM-harvester-service"
LLM_FIELD_MESSAGE = "Filled by the LLM harvester because this value was missing from the source metadata."


def _is_llm_filled(payload: dict) -> bool:
    """
    True if this field's value was written by the LLM enrichment step
    (i.e. apply_llm_res_to_lterlife tagged it), rather than coming from
    the original harvested/mapped record.
    """
    if not isinstance(payload, dict):
        return False
    return LLM_SOURCE_TAG in (payload.get("raw_fields") or [])


def _llm_summary_for_record(mapped_fields: dict) -> dict:
    """
    Build a small, human-readable summary of which LTER-LIFE fields in a
    single mapped record were filled by the LLM harvester.
    """
    llm_filled_fields = [
        field for field, payload in mapped_fields.items()
        if _is_llm_filled(payload)
    ]

    if llm_filled_fields:
        message = (
            f"{len(llm_filled_fields)} field(s) were filled by the LLM harvester: "
            f"{', '.join(llm_filled_fields)}."
        )
    else:
        message = "No fields in this record were filled by the LLM harvester."

    return {
        "llm_filled_fields": llm_filled_fields,
        "llm_filled_count": len(llm_filled_fields),
        "message": message,
    }


# =====================================================
# KNOWN API MAPPINGS
# =====================================================
KNOWN_APIS = {
    "stac.ecodatacube.eu": ("STAC", "https://stac.ecodatacube.eu/api/stac"),
    "data.rivm.nl": ("CSW", "https://data.rivm.nl/meta/srv/dut/csw"),
    "dataverse.nioz.nl": ("OAI-PMH", "https://dataverse.nioz.nl/oai"),
    "dataverse.nl": ("OAI-PMH", "https://dataverse.nl/oai"),
    "lifesciences.datastations.nl": ("OAI-PMH", "https://lifesciences.datastations.nl/oai"),
    "datastations.nl": ("OAI-PMH", "https://lifesciences.datastations.nl/oai"),
    "datahuiswadden.openearth.nl": ("CSW", "https://datahuiswadden.openearth.nl/geonetwork/srv/eng/csw"),
    "zenodo.org": ("Zenodo", "https://zenodo.org/api/records"),
    "api.gbif.org": ("GBIF", "https://api.gbif.org/v1/dataset/search"),
    "gbif.org": ("GBIF", "https://api.gbif.org/v1/dataset/search"),
    "nationaalgeoregister.nl": ("CSW", "https://nationaalgeoregister.nl/geonetwork/srv/eng/csw"),
    "opendata.cbs.nl": ("OData", "https://opendata.cbs.nl/ODataApi/OData/82070NED"),
}

SUPPORTED_MAPPING_HOSTS = {
    "dataverse.nioz.nl",
    "dataverse.nl",
    "lifesciences.datastations.nl",   # DANS
    "datastations.nl",                # DANS
    "datahuiswadden.openearth.nl",    # Datahuis Waddenzee
    "zenodo.org",
    "api.gbif.org",
    "gbif.org",
    "data.rivm.nl",
}


# =====================================================
# SOURCE REGISTRY
# =====================================================
# When the UI passes an explicit `source` id, protocol + endpoint + mapper
# are taken straight from here (no host guessing, and the schema-mapping
# gate is skipped). Free-text URLs still fall back to KNOWN_APIS /
# select_mapper below.
SOURCES = {
    "dataverse_nl": {
        "label": "Dataverse.nl",
        "display_url": "https://dataverse.nl/",
        "api": "https://dataverse.nl/oai",
        "protocol": "OAI-PMH",
        "mapper": "dataverse",
    },
    "dans": {
        "label": "DANS Data Station (Life Sciences)",
        "display_url": "https://lifesciences.datastations.nl/",
        "api": "https://lifesciences.datastations.nl/oai",
        "protocol": "OAI-PMH",
        "mapper": "dans",
    },
    "datahuis_wadden": {
        "label": "Datahuis Waddenzee",
        "display_url": "https://datahuiswadden.openearth.nl/geonetwork/",
        "api": "https://datahuiswadden.openearth.nl/geonetwork/srv/eng/csw",
        "protocol": "CSW",
        "mapper": "iso19139",
    },
    "gbif": {
        "label": "GBIF",
        "display_url": "https://www.gbif.org/",
        "api": "https://api.gbif.org/v1/dataset/search",
        "protocol": "GBIF",
        "mapper": "gbif",
    },
    "zenodo": {
        "label": "Zenodo",
        "display_url": "https://zenodo.org/",
        "api": "https://zenodo.org/api/records",
        "protocol": "Zenodo",
        "mapper": "zenodo",
    },
    "rivm_geonetwork": {
        "label": "RIVM (data.rivm.nl)",
        "display_url": "https://data.rivm.nl/",
        "api": "https://data.rivm.nl/meta/srv/dut/csw",
        "protocol": "CSW",
        "mapper": "iso19139_rivm",
    },
}


def _get_mapper(mapper_key: str):
    return {
        "dataverse": map_record_to_lterlife,
        "dans": map_dans_record_to_lterlife,
        "zenodo": map_zenodo_record_to_lterlife,
        "gbif": map_gbif_record_to_lterlife,
        "iso19139": map_iso19139_record_to_lterlife,
        "iso19139_rivm": map_rivm_record_to_lterlife,
    }.get(mapper_key, map_record_to_lterlife)


# =====================================================
# URL / PROTOCOL HELPERS
# =====================================================
def normalize_base(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def normalize_host(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc.lower().replace("www.", "")


def detect_protocol(url: str):
    base = normalize_base(url)
    host = normalize_host(url)   # was: urlparse(base).netloc.lower()
    return KNOWN_APIS.get(host, ("Unknown", base))


def supports_schema_mapping(url: str) -> bool:
    host = normalize_host(url)
    print(f"🔍 host extracted: {host!r}", flush=True)
    print(f"🔍 supported hosts: {SUPPORTED_MAPPING_HOSTS}", flush=True)
    return host in SUPPORTED_MAPPING_HOSTS


def supports_server_side_keyword_filtering(url: str) -> bool:
    """
    Current Dataverse harvesting uses OAI-PMH endpoints:

      - https://dataverse.nioz.nl/oai
      - https://dataverse.nl/oai

    OAI-PMH does not support general boolean keyword filtering.
    """
    return False


# =====================================================
# GENERIC HELPERS
# =====================================================


def guess_landing_url_from_xml(xml_string: str) -> str | None:
    try:
        root = ET.fromstring(xml_string)
    except Exception:
        return None

    urls = []
    for elem in root.iter():
        if elem.text and "http" in elem.text:
            t = elem.text.strip()
            if t.startswith("http://") or t.startswith("https://"):
                urls.append(t)

    for u in urls:
        if "doi.org" in u:
            return u
    return urls[0] if urls else None


def extract_raw_fields(xml_string: str) -> dict:
    root = ET.fromstring(xml_string)
    fields = defaultdict(list)
    for elem in root.iter():
        if elem.text and elem.text.strip():
            tag = elem.tag.split("}")[-1]
            fields[tag].append(elem.text.strip())
    return fields


def _resolve_effective_limit(max_records, hard_cap: int | None):
    if max_records is None:
        return hard_cap
    try:
        m = int(max_records)
    except Exception:
        raise ValueError("max_records must be an int or None")
    return None if m <= 0 else m


def _is_missing_lter_value(v) -> bool:
    if v is None:
        return True
    if isinstance(v, list):
        return all(_is_missing_lter_value(x) for x in v)
    s = str(v).strip().lower()
    return s in {"", "-", "n/a", "none", "null", "unknown"}


def _get_first_url_from_mapped(mapped_fields: dict) -> str | None:
    """
    Best URL to hand to the LLM harvester service for a record.
    Tries, in order: Landing page -> Access URL -> Identifier (DOI/URL).
    Coverage prefixes ('*' partial, '**' LLM-filled) and 'doi:' scheme are
    normalised away.
    """
    def norm(u) -> str:
        u = (str(u) if u is not None else "").strip()
        while u[:1] == "*":
            u = u[1:].strip()
        if u.lower().startswith("doi:"):
            u = "https://doi.org/" + u[4:].strip()
        if re.match(r"^10\.\d{4,9}/\S+$", u):  # bare DOI
            u = "https://doi.org/" + u
        return u

    for field in ("Landing page", "Access URL", "Identifier"):
        v = mapped_fields.get(field, {}).get("value")
        candidates = v if isinstance(v, list) else [v]
        for it in candidates:
            u = norm(it)
            if u.startswith(("http://", "https://")):
                return u

    return None


# Values that the LLM-harvester-service itself uses to mean "nothing found",
# after stripping its trailing "; " punctuation. Treated the same as missing.
_LLM_EMPTY_VALUES = {"", "-", "n/a", "none", "null", "unknown", "not available", "not found"}


def _clean_llm_text(s) -> Optional[str]:
    """
    The LLM harvester service returns values like 'Example Domain;' or 'N/A;'
    (see its README: every field is terminated with a trailing semicolon).
    Strip that punctuation and collapse whitespace.
    """
    if s is None:
        return None
    s = str(s).strip()
    s = re.sub(r"\s*;+\s*$", "", s).strip()
    return s


def _normalize_llm_value(x):
    if x is None:
        return None
    if isinstance(x, list):
        cleaned = [_clean_llm_text(v) for v in x]
        cleaned = [v for v in cleaned if v and v.lower() not in _LLM_EMPTY_VALUES]
        return cleaned or None
    s = _clean_llm_text(x)
    if not s or s.lower() in _LLM_EMPTY_VALUES:
        return None
    return s


# Maps each field name the LLM-harvester-service actually returns (per its
# README: https://github.com/NLeSC-LTER-LIFE/LLM-metadata-harvester-service)
# to the matching LTER-LIFE schema field name, verified 1:1 against the real
# LTERLIFE_MAPPING keys in converters/lterlife_mapper.py.
#
# Two LTER-LIFE fields have no LLM counterpart and are intentionally absent
# here: "Email of the responsible organization or individual" and
# "levelOfDetail" — the LLM service does not extract either, so they can
# only ever be filled from the original harvested metadata (or stay "-").
LLM_FIELD_TO_LTERLIFE = {
    "Metadata date": ["Date (metadata record)"],
    "Metadata language": ["Language (metadata)"],
    "Responsible organization metadata": ["Responsible party", "Publisher"],
    "Landing page": ["Landing page"],
    "Title": ["Title"],
    "Description": ["Description"],
    "Unique Identifier": ["Identifier"],
    "Resource type": ["Resource type"],
    "Keywords": ["Keyword"],
    "Data creator": ["Creator"],
    "Data contact point": ["Contact point"],
    "Data publisher": ["Publisher"],
    "Spatial coverage": ["Spatial coverage"],
    "Spatial resolution": ["spatialResolutionInMeters"],
    "Spatial reference system": ["Reference system"],
    "Temporal coverage": ["Temporal coverage"],
    "Temporal resolution": ["Temporal resolution"],
    "License": ["License"],
    "Access rights": ["Access rights"],
    "Distribution access URL": ["Access URL"],
    "Distribution format": ["Dataset format (distributionFormat)"],
    "Distribution byte size": ["Size (byte size)"],
}


# Every LTER-LIFE field the LLM harvester service can conceivably fill
# (union of the targets above). Fields outside this set — "Email of the
# responsible organization or individual", "levelOfDetail" — are never
# reachable by enrichment, so they must not, on their own, cause an LLM
# call for a record.
_LLM_FILLABLE_LTER_FIELDS = {
    field for targets in LLM_FIELD_TO_LTERLIFE.values() for field in targets
}


def _llm_fillable_missing_fields(mapped_fields: dict) -> list[str]:
    """LTER-LIFE fields in this record that are (a) fillable by the LLM and
    (b) currently missing a value."""
    return [
        f for f in _LLM_FILLABLE_LTER_FIELDS
        if f in mapped_fields and _is_missing_lter_value(mapped_fields[f].get("value"))
    ]


def apply_llm_res_to_lterlife(mapped_fields: dict, llm_res: dict) -> None:
    """
    Copies values from a successful LLM-harvester-service result into any
    LTER-LIFE fields that are still missing, using the real field names the
    service returns (see LLM_FIELD_TO_LTERLIFE above). Never overwrites a
    field that already has a value from the original harvested record.
    """
    for llm_key, target_fields in LLM_FIELD_TO_LTERLIFE.items():
        if llm_key not in llm_res:
            continue

        raw_val = _normalize_llm_value(llm_res.get(llm_key))
        if raw_val is None:
            continue

        for field in target_fields:
            if field not in mapped_fields:
                continue
            if not _is_missing_lter_value(mapped_fields[field].get("value")):
                continue

            if isinstance(raw_val, list):
                val = [f"**{v}" for v in raw_val]
            else:
                val = f"**{raw_val}"

            mapped_fields[field]["value"] = val
            mapped_fields[field]["raw_fields"] = [LLM_SOURCE_TAG]


def debug_print_missing_fields(mapped_fields: dict, record_idx: int):
    missing = []
    present = []

    for field, payload in mapped_fields.items():
        v = payload.get("value")
        if _is_missing_lter_value(v):
            missing.append(field)
        else:
            present.append(field)

    print(f"\n===== DEBUG FIELD COMPLETENESS for record #{record_idx} =====", flush=True)
    print("Present LTER fields:", present, flush=True)
    print("Missing LTER fields:", missing, flush=True)
    return missing


def debug_print_llm_response(llm_res: dict, record_idx: int):
    print(f"\n===== DEBUG LLM RESPONSE for record #{record_idx} =====", flush=True)

    if not llm_res:
        print("LLM returned empty result.", flush=True)
        return

    print("LLM returned keys:", sorted(llm_res.keys()), flush=True)
    for k, v in llm_res.items():
        print(f"  - {k}: {v}", flush=True)


# =====================================================
# FILTERING ON RAW HARVESTED RECORDS
# =====================================================
def _normalize_filter_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def _xml_record_to_searchable_text(record) -> str:
    """
    Searchable text before mapping.
    Handles both XML strings (OAI/Dataverse) and JSON dicts (Zenodo).
    """
    if isinstance(record, dict):
        text = json.dumps(record, ensure_ascii=False)
    else:
        text = record or ""
    return _normalize_filter_text(text)


def _matches_basic_filter_text(text: str, include_terms: list[str], exclude_terms: list[str]) -> bool:
    if exclude_terms and any(term in text for term in exclude_terms):
        return False

    if include_terms:
        return any(term in text for term in include_terms)

    return True


def _tokenize_query(query: str) -> list[str]:
    return re.findall(r'"[^"]+"|\(|\)|\bAND\b|\bOR\b|\bNOT\b|[^\s()]+', query, flags=re.I)


def _matches_advanced_filter_text(text: str, query: str) -> bool:
    """
    Simple boolean evaluator over raw text.
    Supports:
      AND / OR / NOT / parentheses / quoted phrases
    """
    tokens = _tokenize_query(query)

    if not tokens:
        return True

    expr_parts = []
    prev_is_operand = False  # previous token was a term or ")"
    for tok in tokens:
        upper_tok = tok.upper()
        # Implicit AND: "wadden sea" -> wadden AND sea, "a NOT b" -> a AND NOT b.
        if prev_is_operand and (upper_tok == "NOT" or tok == "(" or upper_tok not in ("AND", "OR", ")")):
            expr_parts.append(" and ")
        prev_is_operand = upper_tok not in ("AND", "OR", "NOT") and tok != "("
        if upper_tok == "AND":
            expr_parts.append(" and ")
        elif upper_tok == "OR":
            expr_parts.append(" or ")
        elif upper_tok == "NOT":
            expr_parts.append(" not ")
        elif tok in ("(", ")"):
            expr_parts.append(tok)
        else:
            term = tok.strip('"').lower()
            expr_parts.append(f'("{term}" in text)')

    expr = "".join(expr_parts)

    try:
        return bool(eval(expr, {"__builtins__": {}}, {"text": text}))
    except Exception:
        print(f"⚠️ Invalid advanced query syntax. Query={query!r}", flush=True)
        return True


def _advanced_query_terms(query: str) -> list[str]:
    """The search terms (not operators/parentheses) of an advanced query, lowercased, de-duplicated."""
    out = []
    for tok in _tokenize_query(query):
        if tok.upper() in ("AND", "OR", "NOT") or tok in ("(", ")"):
            continue
        term = tok.strip('"').lower()
        if term and term not in out:
            out.append(term)
    return out


def apply_filter_to_raw_records(records: list[str], filter_spec: dict | None) -> tuple[list[str], dict]:
    """
    Filtering order in this version:
      harvest -> filter -> map -> LLM enrich kept records only -> export
    """
    total_before = len(records)

    if not filter_spec:
        return records, {
            "mode": None,
            "server_side_applied": False,
            "client_side_applied": False,
            "harvested_before_filtering": total_before,
            "kept_after_filtering": total_before,
        }

    mode = filter_spec.get("mode", "basic")
    include_terms = [t.lower() for t in (filter_spec.get("include_terms") or [])]
    exclude_terms = [t.lower() for t in (filter_spec.get("exclude_terms") or [])]
    query = (filter_spec.get("query") or "").strip()

    if mode == "advanced" and not query:
        return records, {
            "mode": mode,
            "server_side_applied": False,
            "client_side_applied": False,
            "message": "Advanced query mode selected, but query is empty.",
            "harvested_before_filtering": total_before,
            "kept_after_filtering": total_before,
            "include_terms": include_terms,
            "exclude_terms": exclude_terms,
            "query": query,
        }

    # Per-term bookkeeping for the harvest report (see build_term_stats()).
    query_terms = _advanced_query_terms(query) if mode == "advanced" else []
    inc_stats = {t: {"matches": 0, "in_final": 0, "only_reason_kept": 0} for t in include_terms}
    exc_stats = {t: {"matches": 0, "excluded": 0, "only_reason_excluded": 0} for t in exclude_terms}
    adv_stats = {t: {"matches": 0, "in_final": 0} for t in query_terms}
    removed_no_include = 0
    removed_by_exclude = 0

    filtered = []
    for record_xml in records:
        text = _xml_record_to_searchable_text(record_xml)

        if mode == "advanced":
            keep = _matches_advanced_filter_text(text, query)
            for t in query_terms:
                if t in text:
                    adv_stats[t]["matches"] += 1
                    if keep:
                        adv_stats[t]["in_final"] += 1
        else:
            keep = _matches_basic_filter_text(text, include_terms, exclude_terms)
            inc_hits = [t for t in include_terms if t in text]
            exc_hits = [t for t in exclude_terms if t in text]
            passes_include = (not include_terms) or bool(inc_hits)

            for t in inc_hits:
                inc_stats[t]["matches"] += 1
                if keep:
                    inc_stats[t]["in_final"] += 1
            if keep and len(inc_hits) == 1:
                inc_stats[inc_hits[0]]["only_reason_kept"] += 1

            for t in exc_hits:
                exc_stats[t]["matches"] += 1
                if passes_include:
                    exc_stats[t]["excluded"] += 1
            if passes_include and len(exc_hits) == 1:
                exc_stats[exc_hits[0]]["only_reason_excluded"] += 1

            if not passes_include:
                removed_no_include += 1
            elif exc_hits:
                removed_by_exclude += 1

        if keep:
            filtered.append(record_xml)

    if mode == "advanced":
        term_stats = {"query_terms": adv_stats}
    else:
        term_stats = {
            "inclusion_terms": inc_stats,
            "exclusion_terms": exc_stats,
            "removed_no_inclusion_term": removed_no_include,
            "removed_by_exclusion_term": removed_by_exclude,
        }

    return filtered, {
        "mode": mode,
        "server_side_applied": False,
        "client_side_applied": True,
        "message": "Filtering was applied after harvesting and before schema mapping.",
        "harvested_before_filtering": total_before,
        "kept_after_filtering": len(filtered),
        "term_stats": term_stats,
        "include_terms": include_terms,
        "exclude_terms": exclude_terms,
        "query": query,
    }


# =====================================================
# HARVEST REPORT
# =====================================================
def _report_terms(labels: list[str], stats: dict) -> dict:
    """Re-key lowercased term stats by the term as the user typed it."""
    return {label: stats.get(label.lower(), {}) for label in labels}


def build_harvest_report(
    *,
    protocol: str,
    endpoint: str,
    start_date,
    end_date,
    filter_spec: dict | None,
    record_limit: int | None,
    harvest_stats: dict,
    records_downloaded: int,
    filter_info: dict,
) -> dict:
    """
    Statistics on how each filtering layer and each term shaped the result set.
    Counts that a portal cannot report are None rather than guessed.
    """
    filter_spec = filter_spec or {}
    mode = filter_spec.get("mode") or "basic"
    term_stats = filter_info.get("term_stats") or {}
    after_server = harvest_stats.get("after_server_side_filtering")
    pre = harvest_stats.get("pre_download")

    counts = {
        "records_found": harvest_stats.get("records_found"),
        "after_server_side_filtering": after_server,
        "after_client_pre_download_filtering": pre["kept"] if pre and pre.get("applies") else None,
        "records_downloaded": records_downloaded,
        "after_client_post_download_filtering": filter_info.get("kept_after_filtering", records_downloaded),
    }
    counts["final_records"] = counts["after_client_post_download_filtering"]

    notes = []
    if record_limit and after_server is not None and after_server > records_downloaded:
        notes.append(
            f"Only {records_downloaded} of {after_server} server-side matches were downloaded "
            f"because of the record limit ({record_limit}). Client-side counts refer to the "
            f"downloaded records only."
        )
    if not pre or not pre.get("applies"):
        notes.append("No client-side pre-download filter applies to this source/run.")
    if harvest_stats.get("server_side_filter_dropped"):
        notes.append("The portal rejected the filtered request; records were fetched unfiltered.")
    notes.append(
        "Client-side matching is a case-insensitive substring match on the whole raw record "
        "(e.g. 'sea' also matches 'research')."
    )

    report = {
        "report_type": "LTER-LIFE harvest statistics",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "protocol": protocol,
        "endpoint": endpoint,
        "time_interval": {"from": start_date or None, "until": end_date or None},
        "record_limit": record_limit,
        "filtering_method": "advanced_query" if mode == "advanced" else "include_exclude",
        "server_side_request": harvest_stats.get("server_side_request"),
        "counts": counts,
    }

    if mode == "advanced":
        report["advanced_query"] = filter_spec.get("query") or ""
        report["query_terms"] = term_stats.get("query_terms", {})
    else:
        report["inclusion_terms"] = _report_terms(
            filter_spec.get("include_terms") or [], term_stats.get("inclusion_terms", {})
        )
        report["exclusion_terms"] = _report_terms(
            filter_spec.get("exclude_terms") or [], term_stats.get("exclusion_terms", {})
        )
        report["removed_no_inclusion_term"] = term_stats.get("removed_no_inclusion_term", 0)
        report["removed_by_exclusion_term"] = term_stats.get("removed_by_exclusion_term", 0)

    report["term_stat_definitions"] = {
        "matches": "downloaded records containing the term",
        "in_final": "final records containing the term",
        "only_reason_kept": "final records kept only because of this inclusion term",
        "excluded": "records removed that contain this exclusion term (and matched an inclusion term)",
        "only_reason_excluded": "records removed only because of this exclusion term",
    }
    report["notes"] = notes
    return report


# =====================================================
# STRUCTURED PROGRESS HELPER
# =====================================================
def _progress_emit(
    progress_callback: Optional[Callable[..., None]],
    phase_key: str,
    message: str,
    status: Optional[str] = None,
) -> None:
    """
    Structured phase-aware progress emitter.

    Expected callback shape from api.py:
        progress_callback(phase_key, message, status=None)

    Example:
        _progress_emit(progress_callback, "phase3", "Mapping filtered record to LTERLIFE Schema.", "RUNNING")
    """
    print(f"[{phase_key}] {message}", flush=True)

    if progress_callback:
        try:
            progress_callback(phase_key, message, status)
        except TypeError:
            # Backward compatibility if some caller still expects only one arg
            try:
                progress_callback(message)
            except Exception:
                pass
        except Exception:
            pass

def select_mapper(portal_url: str):
    url = portal_url.lower()

    if "zenodo" in url:
        print("🧭 Selected mapper: Zenodo → LTER-LIFE")
        return map_zenodo_record_to_lterlife

    if "gbif" in url:
        print("🧭 Selected mapper: GBIF (EML) → LTER-LIFE")
        return map_gbif_record_to_lterlife

    if "rivm" in url:
        print("🧭 Selected mapper: RIVM (ISO 19139) → LTER-LIFE")
        return map_rivm_record_to_lterlife

    if "datahuiswadden" in url or "datahuis" in url:
        print("🧭 Selected mapper: Datahuis Waddenzee (ISO 19139) → LTER-LIFE")
        return map_iso19139_record_to_lterlife

    if "datastations" in url or "/dans" in url or url.endswith("dans"):
        print("🧭 Selected mapper: DANS (Dublin Core) → LTER-LIFE")
        return map_dans_record_to_lterlife

    print("🧭 Selected mapper: Default/Dataverse → LTER-LIFE")
    return map_record_to_lterlife
# =====================================================
# MAIN ENTRY POINT
# =====================================================
def run_harvest(
    portal_url: str,
    start_date=None,
    end_date=None,
    max_records=None,
    filter_spec=None,
    progress_callback: Optional[Callable[..., None]] = None,
    run_llm: bool = False,          # NEW: LLM enrichment is opt-in now
    llm_api_key: Optional[str] = None,
    source: Optional[str] = None,   # NEW: explicit source id from the UI dropdown
):
    """
    Current pipeline:
      harvest -> filter -> map -> LLM enrich kept records only -> export

    Rules:
    - Only dataverse.nioz.nl and dataverse.nl are supported for schema mapping.
    - For these two URLs, current harvesting uses OAI-PMH.
    - OAI-PMH does not support general keyword boolean server-side filtering.
      Therefore filtering is currently applied after harvest, before mapping.
    """
    progress_messages = []

    host = normalize_host(portal_url)

    source_cfg = SOURCES.get((source or "").strip()) if source else None

    if source_cfg:
        # Explicit source picked in the UI: trust the registry, skip host guessing.
        proto = source_cfg["protocol"]
        api = source_cfg["api"]
        mapper = _get_mapper(source_cfg["mapper"])
        print(f"🧭 Source '{source}' → protocol={proto}, mapper={source_cfg['mapper']}", flush=True)
    else:
        if not supports_schema_mapping(portal_url):
            msg = f"So far the schema mapping is not performed for this URL. [host={host!r}, supported={SUPPORTED_MAPPING_HOSTS}]"
            _progress_emit(progress_callback, "phase3", msg, "ERROR")
            return {
                "xml_records": [],
                "ui_records": [],
                "filter_info": {
                    "mode": filter_spec.get("mode") if filter_spec else None,
                    "server_side_applied": False,
                    "client_side_applied": False,
                    "message": msg,
                },
                "progress_messages": [msg],
            }

        proto, api = detect_protocol(portal_url)
        mapper = select_mapper(portal_url)

    print(f"➡️ Detected protocol: {proto}", flush=True)
    print(f"🔗 API endpoint: {api}", flush=True)

    HARD_CAP = None
    effective_limit = _resolve_effective_limit(max_records, HARD_CAP)

    # REST sources (Zenodo, GBIF, ...) have no natural end when the user
    # sets neither a record limit nor keywords/dates: harvesting the whole
    # catalogue would run for many minutes and Phase 1 would look frozen.
    # Apply a default safety cap in that case.
    DEFAULT_REST_CAPS = {"Zenodo": 50, "GBIF": 200, "STAC": 200, "OData": 200}
    if effective_limit is None and proto in DEFAULT_REST_CAPS:
        effective_limit = DEFAULT_REST_CAPS[proto]
        cap_msg = (
            f"No record limit given for {proto}; applying a default cap of "
            f"{effective_limit}. Set 'Max records', or add keywords / a date "
            f"range, to change this."
        )
        _progress_emit(progress_callback, "phase1", cap_msg)
        progress_messages.append(cap_msg)

    def _harvest_progress(message: str) -> None:
        _progress_emit(progress_callback, "phase1", message)
        progress_messages.append(message)

    server_side_possible = supports_server_side_keyword_filtering(portal_url)
    if filter_spec:
        print(f"🔎 Filter mode: {filter_spec.get('mode')}", flush=True)
        print(f"🔎 Server-side keyword filtering possible: {server_side_possible}", flush=True)

    # =================================================
    # 1) HARVEST PHASE
    # =================================================
    start_harvest_msg = "Connecting to source endpoint."
    _progress_emit(progress_callback, "phase1", start_harvest_msg, "RUNNING")
    progress_messages.append(start_harvest_msg)

    proto_msg = f"Detected protocol: {proto}"
    _progress_emit(progress_callback, "phase1", proto_msg)
    progress_messages.append(proto_msg)

    endpoint_msg = f"Resolved API endpoint: {api}"
    _progress_emit(progress_callback, "phase1", endpoint_msg)
    progress_messages.append(endpoint_msg)

    harvest_stats: dict = {}

    if proto == "OAI-PMH":
        harvest_begin_msg = "Harvesting records through OAI-PMH."
        _progress_emit(progress_callback, "phase1", harvest_begin_msg)
        progress_messages.append(harvest_begin_msg)

        records = harvest_oai(api, effective_limit, start_date, end_date, stats=harvest_stats)
        harvest_msg = f"Total OAI records harvested: {len(records)}"

    elif proto == "CSW":
        harvest_begin_msg = "Harvesting records through CSW (ISO 19139)."
        _progress_emit(progress_callback, "phase1", harvest_begin_msg)
        progress_messages.append(harvest_begin_msg)

        include_terms = (filter_spec or {}).get("include_terms") or []
        exclude_terms = (filter_spec or {}).get("exclude_terms") or []
        records = harvest_csw(
            api, effective_limit, start_date, end_date, include_terms, exclude_terms,
            stats=harvest_stats,
        )
        harvest_msg = f"Total CSW records harvested: {len(records)}"

    elif proto == "GBIF":
        harvest_begin_msg = "Harvesting records through the GBIF REST API."
        _progress_emit(progress_callback, "phase1", harvest_begin_msg)
        progress_messages.append(harvest_begin_msg)

        include_terms = (filter_spec or {}).get("include_terms") or []
        exclude_terms = (filter_spec or {}).get("exclude_terms") or []
        print(f"🔎 Include terms passed to GBIF: {include_terms}", flush=True)
        records = harvest_gbif(
            api, effective_limit, start_date, end_date, include_terms, exclude_terms,
            progress_cb=_harvest_progress,
            stats=harvest_stats,
        )
        harvest_msg = f"Total GBIF records harvested: {len(records)}"

    elif proto == "STAC":
        harvest_begin_msg = "Harvesting records through STAC."
        _progress_emit(progress_callback, "phase1", harvest_begin_msg)
        progress_messages.append(harvest_begin_msg)

        records = harvest_stac(api, effective_limit)
        harvest_msg = f"Total STAC records harvested: {len(records)}"

    elif proto == "GeoNetwork":
        harvest_begin_msg = "Harvesting records through GeoNetwork API."
        _progress_emit(progress_callback, "phase1", harvest_begin_msg)
        progress_messages.append(harvest_begin_msg)

        records = harvest_geonetwork(api, effective_limit)
        harvest_msg = f"Total GeoNetwork records harvested: {len(records)}"

    elif proto == "OData":
        harvest_begin_msg = "Harvesting records through OData."
        _progress_emit(progress_callback, "phase1", harvest_begin_msg)
        progress_messages.append(harvest_begin_msg)

        records = harvest_odata(api, effective_limit)
        harvest_msg = f"Total OData records harvested: {len(records)}"


    elif proto == "Zenodo":

        harvest_begin_msg = "Harvesting records through Zenodo REST API."

        _progress_emit(progress_callback, "phase1", harvest_begin_msg)

        progress_messages.append(harvest_begin_msg)

        include_terms = (filter_spec or {}).get("include_terms") or []

        print(f"🔎 Include terms passed to Zenodo: {include_terms}", flush=True)

        records = harvest_zenodo(
            api, effective_limit, start_date, end_date, include_terms,
            progress_cb=_harvest_progress,
            stats=harvest_stats,
        )

        harvest_msg = f"Total Zenodo records harvested: {len(records)}"




    else:
        msg = "Unsupported protocol."
        print("⚠️ Unsupported protocol", flush=True)
        _progress_emit(progress_callback, "phase1", msg, "ERROR")
        return {
            "xml_records": [],
            "ui_records": [],
            "filter_info": {},
            "progress_messages": [msg],
        }

    _progress_emit(progress_callback, "phase1", harvest_msg)
    progress_messages.append(harvest_msg)

    harvest_before_msg = f"Harvested records before filtering: {len(records)}"
    _progress_emit(progress_callback, "phase1", harvest_before_msg)
    progress_messages.append(harvest_before_msg)

    _progress_emit(progress_callback, "phase1", "Endpoint harvesting finished.", "DONE")
    progress_messages.append("Endpoint harvesting finished.")

    # =================================================
    # 2) FILTER PHASE
    # =================================================
    _progress_emit(progress_callback, "phase2", "Applying filtering rules to harvested raw records.", "RUNNING")
    progress_messages.append("Applying filtering rules to harvested raw records.")

    if filter_spec:
        mode_msg = f"Filter mode: {filter_spec.get('mode', 'basic')}"
        _progress_emit(progress_callback, "phase2", mode_msg)
        progress_messages.append(mode_msg)

        if filter_spec.get("mode") == "basic":
            include_terms = filter_spec.get("include_terms") or []
            exclude_terms = filter_spec.get("exclude_terms") or []

            if include_terms:
                msg = f"Include terms: {', '.join(include_terms)}"
                _progress_emit(progress_callback, "phase2", msg)
                progress_messages.append(msg)

            if exclude_terms:
                msg = f"Exclude terms: {', '.join(exclude_terms)}"
                _progress_emit(progress_callback, "phase2", msg)
                progress_messages.append(msg)
        elif filter_spec.get("mode") == "advanced":
            query = (filter_spec.get("query") or "").strip()
            msg = f"Advanced query: {query if query else '(empty)'}"
            _progress_emit(progress_callback, "phase2", msg)
            progress_messages.append(msg)
    else:
        msg = "No filtering specification provided. All harvested records will be kept."
        _progress_emit(progress_callback, "phase2", msg)
        progress_messages.append(msg)

    filtered_records, filter_info = apply_filter_to_raw_records(records, filter_spec)

    harvest_report = build_harvest_report(
        protocol=proto,
        endpoint=api,
        start_date=start_date,
        end_date=end_date,
        filter_spec=filter_spec,
        record_limit=effective_limit,
        harvest_stats=harvest_stats,
        records_downloaded=len(records),
        filter_info=filter_info,
    )

    kept_msg = f"Records kept after filtering: {len(filtered_records)}"
    _progress_emit(progress_callback, "phase2", kept_msg)
    progress_messages.append(kept_msg)

    _progress_emit(progress_callback, "phase2", "Filtering phase finished.", "DONE")
    progress_messages.append("Filtering phase finished.")

    # =================================================
    # 3) MAPPING PHASE
    # =================================================
    _progress_emit(progress_callback, "phase3", "Mapping filtered records to LTERLIFE Schema.", "RUNNING")
    progress_messages.append("Mapping filtered records to LTERLIFE Schema.")

    prepared_records = []
    important_fields = None

    if not filtered_records:
        msg = "No records remained after filtering. Schema mapping skipped."
        _progress_emit(progress_callback, "phase3", msg)
        progress_messages.append(msg)
    else:
        for idx, raw_record in enumerate(filtered_records, start=1):
            record_msg = f"Mapping filtered record {idx}/{len(filtered_records)} to LTERLIFE Schema."
            _progress_emit(progress_callback, "phase3", record_msg)
            progress_messages.append(record_msg)

            mapped_fields = mapper(raw_record)
            debug_print_missing_fields(mapped_fields, idx)

            if important_fields is None:
                important_fields = list(mapped_fields.keys())
                fields_msg = f"Detected {len(important_fields)} target LTERLIFE fields."
                _progress_emit(progress_callback, "phase3", fields_msg)
                progress_messages.append(fields_msg)

            prepared_records.append({
                "record_xml": raw_record,
                "mapped_fields": mapped_fields,
            })

    mapping_msg = "Schema mapping finished for filtered records."
    _progress_emit(progress_callback, "phase3", mapping_msg, "DONE")
    progress_messages.append(mapping_msg)

    # =================================================
    # 4) LLM ENRICHMENT PHASE (optional, opt-in)
    # =================================================
    llm_candidates = 0
    final_mapped_records = []

    if not run_llm:
        # skip_msg = "LLM enrichment skipped (not requested). Records exported as mapped."
        #  _progress_emit(progress_callback, "phase4", skip_msg, "DONE")
        # progress_messages.append(skip_msg)
        final_mapped_records = [item["mapped_fields"] for item in prepared_records]

    elif not prepared_records:
        msg = "No mapped records available for LLM enrichment."
        _progress_emit(progress_callback, "phase4", msg)
        progress_messages.append(msg)
    else:
        for idx, item in enumerate(prepared_records, start=1):
            record_xml = item["record_xml"]
            mapped_fields = item["mapped_fields"]

            any_missing = bool(_llm_fillable_missing_fields(mapped_fields))

            if any_missing:
                llm_candidates += 1
                _progress_emit(
                    progress_callback,
                    "phase4",
                    f"Record {idx}/{len(prepared_records)} has missing fields and is selected for LLM enrichment."
                )
                progress_messages.append(
                    f"Record {idx}/{len(prepared_records)} has missing fields and is selected for LLM enrichment."
                )

                landing_url = _get_first_url_from_mapped(mapped_fields)

                if not landing_url:
                    landing_url = guess_landing_url_from_xml(record_xml)

                if landing_url:
                    landing_msg = f"Calling LLM harvester for landing URL: {landing_url}"
                    _progress_emit(progress_callback, "phase4", landing_msg)
                    progress_messages.append(landing_msg)

                    try:
                        llm = enrich_url(landing_url)
                        llm_res = llm.get("result", {})
                        debug_print_llm_response(llm_res, idx)

                        apply_llm_res_to_lterlife(mapped_fields, llm_res)

                        mapped_fields["LLM_enrichment_source_url"] = {
                            "value": landing_url,
                            "raw_fields": [LLM_SOURCE_TAG],
                        }

                        # Per-record summary of which fields the LLM filled,
                        # so both the backend logs and the UI can show it.
                        llm_summary = _llm_summary_for_record(mapped_fields)
                        summary_msg = f"Record {idx}/{len(prepared_records)}: {llm_summary['message']}"
                        _progress_emit(progress_callback, "phase4", summary_msg)
                        progress_messages.append(summary_msg)

                        success_msg = f"LLM enrichment finished for record {idx}/{len(prepared_records)}."
                        _progress_emit(progress_callback, "phase4", success_msg)
                        progress_messages.append(success_msg)

                    except Exception as e:
                        err_msg = f"LLM enrichment failed for record {idx}/{len(prepared_records)}: {type(e).__name__}: {e}"
                        _progress_emit(progress_callback, "phase4", err_msg)
                        progress_messages.append(err_msg)

                        mapped_fields["LLM_enrichment_error"] = {
                            "value": f"{type(e).__name__}: {e}",
                            "raw_fields": [LLM_SOURCE_TAG],
                        }
                else:
                    no_url_msg = f"No landing page URL found for record {idx}/{len(prepared_records)}. LLM enrichment skipped."
                    _progress_emit(progress_callback, "phase4", no_url_msg)
                    progress_messages.append(no_url_msg)
            else:
                not_needed_msg = f"Record {idx}/{len(prepared_records)} has no missing fields. LLM enrichment not needed."
                _progress_emit(progress_callback, "phase4", not_needed_msg)
                progress_messages.append(not_needed_msg)

            final_mapped_records.append(mapped_fields)

    if llm_candidates > 0:
        llm_msg = f"LLM enrichment was used for {llm_candidates} filtered records with missing fields."
    else:
        llm_msg = "LLM enrichment was not needed after filtering."

    _progress_emit(progress_callback, "phase4", llm_msg, "DONE")
    progress_messages.append(llm_msg)

    # =================================================
    # 5) BUILD UI OUTPUT
    # =================================================
    # Each record now gets:
    #   - a "record_summary" row listing which LTER-LIFE fields (if any)
    #     were filled by the LLM harvester, with a human-readable message
    #   - per-field rows carrying "filled_by_llm" (bool) and "llm_message"
    #     so the frontend can badge/highlight exactly which fields came
    #     from the LLM rather than the original source metadata.
    output_records = []
    for idx, mapped_fields in enumerate(final_mapped_records):
        llm_summary = _llm_summary_for_record(mapped_fields)
        output_records.append({
            "record_summary": True,
            "record_index": idx + 1,
            "llm_filled_fields": llm_summary["llm_filled_fields"],
            "llm_filled_count": llm_summary["llm_filled_count"],
            "message": llm_summary["message"],
        })

        for lter_field, payload in mapped_fields.items():
            llm_filled = _is_llm_filled(payload)
            output_records.append({
                "lterlife_field": lter_field,
                "raw_field": ", ".join(payload.get("raw_fields", [])),
                "value": payload.get("value", ""),
                "filled_by_llm": llm_filled,
                "llm_message": LLM_FIELD_MESSAGE if llm_filled else None,
            })

        if idx < len(final_mapped_records) - 1:
            output_records.append({"separator": True})

    # =================================================
    # 6) EXPORT PHASE
    # =================================================
    _progress_emit(progress_callback, "phase5", "Exporting final mapped records to JSON-LD.", "RUNNING")
    progress_messages.append("Exporting final mapped records to JSON-LD.")

    base_dir = os.path.dirname(os.path.abspath(__file__))
    jsonld_dir = os.path.join(base_dir, "jsonld")
    zip_path = os.path.join(base_dir, "jsonld_records.zip")

    export_jsonld_records(records=final_mapped_records, output_dir=jsonld_dir, context=JSONLD_CONTEXT)
    export_jsonld_msg = f"JSON-LD records exported."
    _progress_emit(progress_callback, "phase5", export_jsonld_msg)
    progress_messages.append(export_jsonld_msg)

    create_zip(jsonld_dir, zip_path)
    zip_msg = f"ZIP package created. "
    _progress_emit(progress_callback, "phase5", zip_msg)
    progress_messages.append(zip_msg)

    export_msg = "Export finished."
    _progress_emit(progress_callback, "phase5", export_msg, "DONE")
    progress_messages.append(export_msg)

    return {
        "xml_records": filtered_records,
        "ui_records": output_records,
        "filter_info": filter_info,
        "progress_messages": progress_messages,
        "mapped_records": final_mapped_records,  # NEW: reusable for later LLM enrichment
        "harvest_report": harvest_report,
    }

# =====================================================
# STANDALONE LLM ENRICHMENT (called after harvest, on demand)
# =====================================================
def enrich_and_reexport(
    mapped_records: list[dict],
    api_key: Optional[str] = None,
    progress_callback: Optional[Callable[..., None]] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """
    Takes already-mapped LTER-LIFE records (from a previous run_harvest call),
    runs LLM enrichment on the ones with missing fields, re-exports JSON-LD,
    and returns the updated UI records + mapped records.
    """
    from llm_harvester_client import enrich_url  # local import avoids unused import when unused

    total = len(mapped_records)
    llm_candidates = 0
    enriched_count = 0

    for idx, mapped_fields in enumerate(mapped_records, start=1):
        # Records may already carry bookkeeping keys from a previous enrich
        # pass; strip them so a re-run starts clean.
        mapped_fields.pop("LLM_enrichment_error", None)

        missing_fields = _llm_fillable_missing_fields(mapped_fields)

        if not missing_fields:
            _progress_emit(progress_callback, "phase4",
                           f"Record {idx}/{total} has no LLM-fillable missing fields.")
        else:
            llm_candidates += 1
            landing_url = _get_first_url_from_mapped(mapped_fields)
            if not landing_url:
                _progress_emit(
                    progress_callback, "phase4",
                    f"Record {idx}/{total}: {len(missing_fields)} missing field(s) "
                    f"but no landing/access URL to harvest — LLM skipped.",
                )
            else:
                _progress_emit(
                    progress_callback, "phase4",
                    f"Record {idx}/{total}: calling LLM harvester for {landing_url} "
                    f"({len(missing_fields)} missing field(s)).",
                )
                try:
                    llm = enrich_url(landing_url, api_key=api_key)
                    before = set(_llm_fillable_missing_fields(mapped_fields))
                    apply_llm_res_to_lterlife(mapped_fields, llm.get("result", {}))
                    after = set(_llm_fillable_missing_fields(mapped_fields))
                    filled_now = sorted(before - after)

                    mapped_fields["LLM_enrichment_source_url"] = {
                        "value": landing_url,
                        "raw_fields": [LLM_SOURCE_TAG],
                    }
                    if filled_now:
                        enriched_count += 1

                    _progress_emit(
                        progress_callback, "phase4",
                        f"Record {idx}/{total}: LLM filled {len(filled_now)} field(s)"
                        + (f": {', '.join(filled_now)}." if filled_now
                           else " (LLM returned nothing usable for the missing fields)."),
                    )
                except Exception as e:
                    err_msg = f"LLM enrichment failed for record {idx}: {type(e).__name__}: {e}"
                    _progress_emit(progress_callback, "phase4", err_msg)
                    mapped_fields["LLM_enrichment_error"] = {"value": err_msg, "raw_fields": [LLM_SOURCE_TAG]}

        if on_progress:
            try:
                on_progress(idx, len(mapped_records))
            except Exception:
                pass

    _progress_emit(
        progress_callback, "phase4",
        f"LLM enrichment finished: {llm_candidates}/{total} record(s) had missing "
        f"fields, {enriched_count} were filled with at least one LLM value.",
        "DONE",
    )

    # Rebuild UI output (same shape as run_harvest's phase 5), including the
    # per-record LLM summary and per-field filled_by_llm/llm_message flags.
    output_records = []
    for idx, mapped_fields in enumerate(mapped_records):
        llm_summary = _llm_summary_for_record(mapped_fields)
        output_records.append({
            "record_summary": True,
            "record_index": idx + 1,
            "llm_filled_fields": llm_summary["llm_filled_fields"],
            "llm_filled_count": llm_summary["llm_filled_count"],
            "message": llm_summary["message"],
        })

        for lter_field, payload in mapped_fields.items():
            llm_filled = _is_llm_filled(payload)
            output_records.append({
                "lterlife_field": lter_field,
                "raw_field": ", ".join(payload.get("raw_fields", [])),
                "value": payload.get("value", ""),
                "filled_by_llm": llm_filled,
                "llm_message": LLM_FIELD_MESSAGE if llm_filled else None,
            })
        if idx < len(mapped_records) - 1:
            output_records.append({"separator": True})

    # Re-export JSON-LD + ZIP with enriched data
    base_dir = os.path.dirname(os.path.abspath(__file__))
    jsonld_dir = os.path.join(base_dir, "jsonld")
    zip_path = os.path.join(base_dir, "jsonld_records.zip")

    export_jsonld_records(records=mapped_records, output_dir=jsonld_dir, context=JSONLD_CONTEXT)
    create_zip(jsonld_dir, zip_path)

    _progress_emit(progress_callback, "phase5", "Re-exported enriched records to JSON-LD.", "DONE")

    return {
        "ui_records": output_records,
        "mapped_records": mapped_records,
    }

if __name__ == "__main__":
    print("Run through FastAPI UI only.")