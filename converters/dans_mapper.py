# converters/dans_mapper.py
# ------------------------------------------------------
# DANS Data Station (OAI-PMH, oai_dc / Dublin Core)  ->  LTER-LIFE
# ------------------------------------------------------
# The DANS Data Stations (e.g. https://lifesciences.datastations.nl/oai)
# expose harvested records as simple Dublin Core (oai_dc). This mapper
# follows the coverage decisions in DANS_lterlife_template_mapping_Parinaz.xlsx,
# expressed against the (namespace-stripped, lowercased) DC element names.
#
# Output contract matches converters/lterlife_mapper.py:
#   - exact coverage   -> value as-is
#   - partial coverage -> value prefixed with '*'
#   - no coverage      -> '-'

import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Dict, List, Union


DANS_MAPPING: Dict[str, Dict[str, Union[List[str], str]]] = {
    "Date (metadata record)": {"sources": ["date"], "coverage": "partial"},
    "Language (metadata)": {"sources": ["language"], "coverage": "exact"},
    "Responsible party": {"sources": ["creator", "contributor"], "coverage": "partial"},
    "Email of the responsible organization or individual": {
        "sources": ["email"], "coverage": "partial",
    },
    "Landing page": {"sources": ["identifier"], "coverage": "partial"},
    "Title": {"sources": ["title"], "coverage": "exact"},
    "Description": {"sources": ["description"], "coverage": "exact"},
    "Identifier": {"sources": ["identifier"], "coverage": "exact"},
    "Resource type": {"sources": ["type"], "coverage": "exact"},
    "Keyword": {"sources": ["subject"], "coverage": "exact"},
    "Creator": {"sources": ["creator"], "coverage": "exact"},
    "Contact point": {"sources": ["contributor"], "coverage": "partial"},
    "Publisher": {"sources": ["publisher"], "coverage": "exact"},

    "spatialResolutionInMeters": {"sources": [], "coverage": "none"},
    "Spatial coverage": {"sources": ["coverage", "spatial"], "coverage": "partial"},
    "Reference system": {"sources": ["spatial"], "coverage": "partial"},

    "Temporal coverage": {"sources": ["temporal", "coverage"], "coverage": "partial"},
    "Temporal resolution": {"sources": [], "coverage": "none"},

    "License": {"sources": ["rights", "license"], "coverage": "exact"},
    "Access rights": {"sources": ["accessrights", "rights"], "coverage": "partial"},
    "Access URL": {"sources": ["identifier"], "coverage": "partial"},

    "Dataset format (distributionFormat)": {"sources": ["format"], "coverage": "partial"},
    "Size (byte size)": {"sources": ["extent"], "coverage": "none"},
}


def _strip_ns(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _collect_fields(root: ET.Element) -> Dict[str, List[str]]:
    fields = defaultdict(list)
    for elem in root.iter():
        tag = _strip_ns(elem.tag).lower()
        text = (elem.text or "").strip()
        if text:
            fields[tag].append(text)
    return fields


def _normalize_identifier(val: str) -> str:
    """DANS DC identifiers look like 'doi:10.17026/DANS-XXX' or a bare URN.
    Turn DOIs into resolvable URLs so Landing page / Access URL are usable."""
    v = val.strip()
    m = re.match(r"^doi:\s*(10\..+)$", v, flags=re.I)
    if m:
        return f"https://doi.org/{m.group(1)}"
    m = re.match(r"^(urn:nbn:.+)$", v, flags=re.I)
    if m:
        return f"https://www.persistent-identifier.nl/{m.group(1)}"
    return v


def map_dans_record_to_lterlife(xml_string: str) -> Dict[str, Union[str, List[str]]]:
    """Maps one DANS oai_dc XML record into the LTER-LIFE schema."""
    try:
        root = ET.fromstring(xml_string)
        extracted = _collect_fields(root)
    except ET.ParseError:
        return {field: {"value": "-", "raw_fields": ["-"]} for field in DANS_MAPPING}

    mapped: Dict[str, Union[str, List[str]]] = {}

    for lter_field, cfg in DANS_MAPPING.items():
        sources = cfg["sources"]
        coverage = cfg["coverage"]
        is_url_field = lter_field in ("Landing page", "Access URL")

        values: List[str] = []
        for src in sources:
            for val in extracted.get(src, []):
                if src == "identifier":
                    val = _normalize_identifier(val)
                    if is_url_field and not val.lower().startswith(("http://", "https://")):
                        continue
                values.append(f"*{val}" if coverage == "partial" else val)

        seen = set()
        values = [v for v in values if not (v in seen or seen.add(v))]

        if values:
            mapped[lter_field] = {
                "value": values if len(values) > 1 else values[0],
                "raw_fields": sources,
            }
        else:
            mapped[lter_field] = {"value": "-", "raw_fields": ["-"]}

    return mapped
