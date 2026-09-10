# converters/gbif_mapper.py
# ------------------------------------------------------
# GBIF (EML)  →  LTER-LIFE Metadata Schema Converter
# ------------------------------------------------------
#
# GBIF dataset metadata (via IPT / OAI-PMH) is served as EML XML,
# not JSON like Zenodo. So unlike zenodo_mapper.py (dict + _flatten),
# this mapper follows the same generic tag-flattening approach as
# lterlife_mapper.py: collect every XML element by its (namespace-
# stripped, lowercased) tag name, then pull LTER-LIFE fields from a
# config dict of candidate source tags + a coverage label.
#
# Coverage semantics match zenodo_mapper.py / lterlife_mapper.py:
#   - exact coverage   -> value as-is
#   - partial coverage -> value prefixed with '*'
#   - no coverage      -> '-'

import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Dict, List, Union

# Hosts that serve licence text, not dataset landing pages. A <url> pointing
# here must never be used as Landing page / Access URL for LLM enrichment.
_LICENSE_HOSTS = ("spdx.org", "creativecommons.org", "opensource.org",
                  "opendatacommons.org")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")


# =====================================================
# LTER-LIFE <- GBIF (EML) mapping with coverage semantics
# =====================================================
#
# Source tag names below are EML element names, namespace-stripped
# and lowercased (matching how _collect_fields() stores them).

GBIF_MAPPING: Dict[str, Dict[str, Union[List[str], str]]] = {
    "Date (metadata record)": {
        "sources": ["datestamp", "pubdate"],
        "coverage": "partial",
    },
    "Language (metadata)": {
        "sources": ["language", "metadatalanguage"],
        "coverage": "partial",
    },
    "Responsible party": {
        "sources": ["organizationname", "surname", "givenname", "positionname"],
        "coverage": "partial",
    },
    "Email of the responsible organization or individual": {
        "sources": ["electronicmailaddress"],
        "coverage": "partial",
    },
    "Landing page": {
        # alternateIdentifier first: in GBIF EML it holds the IPT/portal
        # resource page and/or the dataset DOI. A bare <url> often points at
        # the licence page (spdx.org / creativecommons.org), which is useless
        # for enrichment, so it comes last and licence hosts are filtered out
        # in map_gbif_record_to_lterlife().
        "sources": ["alternateidentifier", "gbif_landing", "url", "online", "distribution@href", "online@href"],
        "coverage": "partial",
    },
    "Title": {
        "sources": ["title"],
        "coverage": "exact",
    },
    "Description": {
        "sources": ["abstract", "para"],
        "coverage": "partial",
    },
    "Identifier": {
        "sources": ["alternateidentifier", "identifier"],
        "coverage": "partial",
    },
    "Resource type": {
        "sources": ["datasettype", "datasetsubtype", "resourcetype"],
        "coverage": "partial",
    },
    "Keyword": {
        "sources": ["keyword"],
        "coverage": "partial",
    },
    "Creator": {
        "sources": ["organizationname", "surname", "givenname"],
        "coverage": "exact",
    },
    "Contact point": {
        "sources": ["electronicmailaddress", "positionname", "organizationname"],
        "coverage": "exact",
    },
    "Publisher": {
        "sources": ["organizationname"],
        "coverage": "exact",
    },

    # ---------- GEOSPATIAL ----------
    # NOTE: field-name keys are kept identical to converters/lterlife_mapper.py
    # so JSON-LD export and LLM enrichment field mapping stay source-agnostic.
    "spatialResolutionInMeters": {"sources": [], "coverage": "none"},
    "Spatial coverage": {
        "sources": [
            "geographicdescription",
            "westboundingcoordinate",
            "eastboundingcoordinate",
            "northboundingcoordinate",
            "southboundingcoordinate",
        ],
        "coverage": "partial",
    },
    "Reference system": {"sources": [], "coverage": "none"},

    # ---------- TEMPORAL ----------
    "Temporal coverage": {
        "sources": ["begindate", "enddate", "calendardate", "singledatetime"],
        "coverage": "exact",
    },
    "Temporal resolution": {"sources": [], "coverage": "none"},

    # ---------- TERMS ----------
    "License": {
        "sources": ["licensename", "citetitle", "identifier"],
        "coverage": "partial",
    },
    "Access rights": {
        "sources": ["intellectualrights", "para"],
        "coverage": "partial",
    },

    # ---------- DISTRIBUTION ----------
    "Access URL": {
        "sources": ["online", "url", "gbif_landing"],
        "coverage": "partial",
    },
    "Dataset format (distributionFormat)": {
        "sources": ["dataformat"],
        "coverage": "partial",
    },
    "Size (byte size)": {
        "sources": ["size"],
        "coverage": "partial",
    },
}


# =====================================================
# Helpers
# =====================================================

def _strip_ns(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _collect_fields(root: ET.Element) -> Dict[str, List[str]]:
    """
    Flattens every XML element in the EML record into
    {lowercased_tag_name: [text values]}, ignoring namespaces.
    Also captures element attributes (e.g. boundingCoordinates'
    child elements are plain text, but some EML fields carry
    values as attributes rather than element text).
    """
    fields = defaultdict(list)

    for elem in root.iter():
        tag = _strip_ns(elem.tag).lower()
        text = (elem.text or "").strip()
        if text:
            fields[tag].append(text)

        for attr_name, attr_val in elem.attrib.items():
            if attr_val and attr_val.strip():
                attr_key = f"{tag}@{_strip_ns(attr_name).lower()}"
                fields[attr_key].append(attr_val.strip())

    return fields


# =====================================================
# Public API
# =====================================================

def map_gbif_record_to_lterlife(xml_string: str) -> Dict[str, Union[str, List[str]]]:
    """
    Maps a harvested GBIF (EML) XML metadata record into the LTER-LIFE schema.

    Rules (same contract as zenodo_mapper.py / lterlife_mapper.py):
    - All LTER-LIFE fields are always present
    - Exact coverage   -> value as-is
    - Partial coverage -> prepend '*'
    - No coverage      -> '-'
    - Multiple values  -> list
    """
    try:
        root = ET.fromstring(xml_string)
        extracted = _collect_fields(root)
    except ET.ParseError:
        return {
            field: {"value": "-", "raw_fields": ["-"]}
            for field in GBIF_MAPPING
        }

    # Synthesize a reliable dataset landing page from the EML packageId
    # (which GBIF sets to the dataset key). Also normalise alternateIdentifier
    # DOIs to resolvable URLs.
    pkg = (extracted.get("eml@packageid") or [""])[0].strip()
    if _UUID_RE.match(pkg):
        extracted.setdefault("gbif_landing", []).insert(0, f"https://www.gbif.org/dataset/{pkg}")
    for i, alt in enumerate(list(extracted.get("alternateidentifier", []))):
        if _DOI_RE.match(alt.strip()):
            extracted["alternateidentifier"][i] = f"https://doi.org/{alt.strip()}"

    def _is_bad_landing(u: str) -> bool:
        u = u.lstrip("*").strip().lower()
        return (not u.startswith(("http://", "https://"))) or any(h in u for h in _LICENSE_HOSTS)

    mapped: Dict[str, Union[str, List[str]]] = {}

    for lter_field, cfg in GBIF_MAPPING.items():
        sources = cfg["sources"]
        coverage = cfg["coverage"]
        drop_license_hosts = lter_field in ("Landing page", "Access URL")

        values: List[str] = []

        for src in sources:
            if src in extracted:
                for val in extracted[src]:
                    if drop_license_hosts and _is_bad_landing(val):
                        continue
                    if coverage == "partial":
                        values.append(f"*{val}")
                    else:
                        values.append(val)

        if values:
            mapped[lter_field] = {
                "value": values if len(values) > 1 else values[0],
                "raw_fields": sources,
            }
        else:
            mapped[lter_field] = {
                "value": "-",
                "raw_fields": ["-"],
            }

    return mapped