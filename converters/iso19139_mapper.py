# converters/iso19139_mapper.py
# ------------------------------------------------------
# ISO 19139 (gmd:MD_Metadata)  ->  LTER-LIFE Metadata Schema Converter
# ------------------------------------------------------
# Shared by Datahuis Waddenzee and RIVM (data.rivm.nl), both served as
# ISO 19139 through a GeoNetwork CSW endpoint.
#
# ISO 19139 wraps almost every value in a typed child element
# (gco:CharacterString, gmx:Anchor, gco:Decimal, gco:Date, gco:DateTime,
# gmd:*Code with a codeListValue attribute). A naive tag->text collector
# would therefore file everything under "characterstring" / "anchor" / ...
# So _collect_fields() is PARENT-AWARE: when an element's own text is empty
# but it holds a value-wrapper child, the child's text (or codeListValue)
# is recorded under the PARENT's local tag name.
#
# Output contract matches converters/lterlife_mapper.py:
#   - exact coverage   -> value as-is
#   - partial coverage -> value prefixed with '*'
#   - no coverage      -> '-'

import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Dict, List, Union


# gco:/gmx: value-wrapper leaf elements whose text belongs to their parent.
_VALUE_WRAPPERS = {
    "characterstring", "anchor", "decimal", "integer", "real",
    "date", "datetime", "boolean", "distance", "measure",
    "localname", "scopedname", "record", "url",
}


def _strip_ns(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _collect_fields(root: ET.Element) -> Dict[str, List[str]]:
    fields: Dict[str, List[str]] = defaultdict(list)

    for elem in root.iter():
        tag = _strip_ns(elem.tag).lower()
        text = (elem.text or "").strip()

        # 1) direct element text (covers gml:beginPosition, gmd:URL, ...)
        if text:
            fields[tag].append(text)

        # 2) *Code elements: value is in codeListValue (fallback to text)
        if tag.endswith("code"):
            code_val = elem.attrib.get("codeListValue") or text
            if code_val:
                fields[tag].append(code_val.strip())

        # 3) parent-aware: pull value-wrapper / *Code children up to this tag
        for child in list(elem):
            ctag = _strip_ns(child.tag).lower()
            ctext = (child.text or "").strip()

            if ctag in _VALUE_WRAPPERS and ctext:
                fields[tag].append(ctext)
            elif ctag.endswith("code"):
                cv = child.attrib.get("codeListValue") or ctext
                if cv:
                    fields[tag].append(cv.strip())

        # 4) useful attributes (xlink:href on linkages/anchors)
        for attr_name, attr_val in elem.attrib.items():
            aname = _strip_ns(attr_name).lower()
            if aname in ("href",) and attr_val and attr_val.strip():
                fields[f"{tag}@href"].append(attr_val.strip())

    # de-dupe while preserving order
    for k, vals in list(fields.items()):
        seen = set()
        deduped = []
        for v in vals:
            if v not in seen:
                seen.add(v)
                deduped.append(v)
        fields[k] = deduped

    return fields


# =====================================================
# LTER-LIFE <- ISO 19139 mapping
# Coverage per LTER-LIFE_Datahuis_Wadden_mapping_REVIEWED_corrected.xlsx
# =====================================================
ISO19139_MAPPING: Dict[str, Dict[str, Union[List[str], str]]] = {
    "Date (metadata record)": {"sources": ["datestamp", "date"], "coverage": "exact"},
    "Language (metadata)": {"sources": ["languagecode", "language"], "coverage": "exact"},
    "Responsible party": {
        "sources": ["organisationname", "individualname", "pointofcontact", "positionname"],
        "coverage": "exact",
    },
    "Email of the responsible organization or individual": {
        "sources": ["electronicmailaddress"], "coverage": "partial",
    },
    "Landing page": {
        "sources": ["url", "linkage", "linkage@href"], "coverage": "partial",
    },
    "Title": {"sources": ["title"], "coverage": "exact"},
    "Description": {"sources": ["abstract"], "coverage": "exact"},
    "Identifier": {
        "sources": ["fileidentifier", "identifier", "md_identifier"], "coverage": "partial",
    },
    "Resource type": {
        "sources": ["hierarchylevel", "md_scopecode"], "coverage": "exact",
    },
    "Keyword": {
        "sources": ["keyword", "md_topiccategorycode", "topiccategory"], "coverage": "exact",
    },
    "Creator": {
        "sources": ["organisationname", "individualname"], "coverage": "partial",
    },
    "Contact point": {
        "sources": ["pointofcontact", "individualname", "organisationname"], "coverage": "exact",
    },
    "Publisher": {
        "sources": ["distributorcontact", "organisationname"], "coverage": "partial",
    },

    # ---------- GEOSPATIAL ----------
    "spatialResolutionInMeters": {
        "sources": ["distance", "denominator"], "coverage": "partial",
    },
    "Spatial coverage": {
        "sources": [
            "westboundlongitude", "eastboundlongitude",
            "southboundlatitude", "northboundlatitude",
            "geographicidentifier", "geographicdescription",
        ],
        "coverage": "exact",
    },
    "Reference system": {
        "sources": ["code", "referencesystemidentifier", "rs_identifier"], "coverage": "exact",
    },

    # ---------- TEMPORAL ----------
    "Temporal coverage": {
        "sources": ["beginposition", "endposition", "timeperiod", "temporalextent"],
        "coverage": "partial",
    },
    "Temporal resolution": {"sources": [], "coverage": "none"},

    # ---------- TERMS ----------
    "License": {
        "sources": ["useconstraints", "otherconstraints", "uselimitation"], "coverage": "partial",
    },
    "Access rights": {
        "sources": ["accessconstraints", "md_restrictioncode", "otherconstraints"],
        "coverage": "partial",
    },

    # ---------- DISTRIBUTION ----------
    "Access URL": {
        "sources": ["url", "linkage", "linkage@href"], "coverage": "exact",
    },
    "Dataset format (distributionFormat)": {
        "sources": ["distributionformat", "name", "version"], "coverage": "exact",
    },
    "Size (byte size)": {"sources": ["transfersize"], "coverage": "none"},
}


def _map_with(mapping: Dict[str, Dict], xml_string: str) -> Dict[str, Union[str, List[str]]]:
    try:
        root = ET.fromstring(xml_string)
        extracted = _collect_fields(root)
    except ET.ParseError:
        return {field: {"value": "-", "raw_fields": ["-"]} for field in mapping}

    mapped: Dict[str, Union[str, List[str]]] = {}

    for lter_field, cfg in mapping.items():
        sources = cfg["sources"]
        coverage = cfg["coverage"]

        values: List[str] = []
        for src in sources:
            for val in extracted.get(src, []):
                values.append(f"*{val}" if coverage == "partial" else val)

        # de-dupe
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


def map_iso19139_record_to_lterlife(xml_string: str) -> Dict[str, Union[str, List[str]]]:
    """Maps one ISO 19139 (gmd:MD_Metadata) XML record into the LTER-LIFE schema."""
    return _map_with(ISO19139_MAPPING, xml_string)
