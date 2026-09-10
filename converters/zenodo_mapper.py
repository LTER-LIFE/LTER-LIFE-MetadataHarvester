# converters/zenodo_mapper.py
from typing import Dict, List, Union

ZENODO_MAPPING: Dict[str, Dict] = {
    "Date (metadata record)": {
        "sources": ["created", "modified"],
        "coverage": "partial",
    },
    "Language (metadata)": {
        "sources": ["language"],
        "coverage": "exact",
    },
    "Responsible party": {
        "sources": ["contributors"],   # list of {name, type, ...}
        "coverage": "partial",
    },
    "Email of the responsible organization or individual": {
        "sources": [],
        "coverage": "none",
    },
    "Landing page": {
        "sources": ["links.self_html", "related_identifiers"],
        "coverage": "partial",
    },
    "Title": {
        "sources": ["title"],
        "coverage": "exact",
    },
    "Description": {
        "sources": ["description"],
        "coverage": "exact",
    },
    "Identifier": {
        "sources": ["doi", "related_identifiers"],
        "coverage": "partial",
    },
    "Resource type": {
        "sources": ["resource_type.type", "upload_type"],
        "coverage": "exact",
    },
    "Keyword": {
        "sources": ["keywords"],
        "coverage": "exact",
    },
    "Creator": {
        "sources": ["creators"],
        "coverage": "exact",
    },
    "Contact point": {
        "sources": ["contributors"],
        "coverage": "partial",
    },
    "Publisher": {
        "sources": ["publisher"],
        "coverage": "partial",
    },
    "Spatial coverage": {
        "sources": ["locations"],
        "coverage": "partial",
    },
    "Temporal coverage": {
        "sources": ["dates"],
        "coverage": "partial",
    },
    "Temporal resolution":  {"sources": [], "coverage": "none"},
    # NOTE: field-name keys below are kept identical to converters/lterlife_mapper.py
    # so downstream code (JSON-LD export, LLM enrichment field mapping) is
    # source-agnostic.
    "spatialResolutionInMeters": {"sources": [], "coverage": "none"},
    "Reference system":     {"sources": [], "coverage": "none"},
    "License": {
        "sources": ["license"],
        "coverage": "exact",
    },
    "Access rights": {
        "sources": ["access_right", "visibility"],
        "coverage": "partial",
    },
    "Access URL": {
        "sources": ["links.self", "links.files"],
        "coverage": "partial",
    },
    "Dataset format (distributionFormat)": {
        "sources": ["files"],      # file extension/MIME from files[]
        "coverage": "partial",
    },
    "Size (byte size)": {
        "sources": ["files"],      # file-level size from files[]
        "coverage": "partial",
    },
}


def _flatten(data: dict, prefix="") -> Dict[str, List[str]]:
    """
    Flatten a nested Zenodo JSON record into dot-notation keys → list of string values.
    e.g. {"resource_type": {"type": "dataset"}} → {"resource_type.type": ["dataset"]}
    Lists of dicts (creators, contributors, keywords, files) are handled specially.
    """
    out: Dict[str, List[str]] = {}

    for k, v in data.items():
        full_key = f"{prefix}.{k}" if prefix else k

        if isinstance(v, str) and v.strip():
            out.setdefault(full_key, []).append(v.strip())

        elif isinstance(v, (int, float)):
            out.setdefault(full_key, []).append(str(v))

        elif isinstance(v, dict):
            nested = _flatten(v, prefix=full_key)
            for nk, nv in nested.items():
                out.setdefault(nk, []).extend(nv)

        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str) and item.strip():
                    out.setdefault(full_key, []).append(item.strip())
                elif isinstance(item, dict):
                    # For named entities, prefer "name" key as representative value
                    name = (item.get("name") or item.get("title") or
                            item.get("id") or str(item))
                    out.setdefault(full_key, []).append(str(name).strip())
    return out


def map_zenodo_record_to_lterlife(record: dict) -> Dict[str, Union[str, List[str]]]:
    """
    Maps a single Zenodo REST API record (Python dict) into the LTER-LIFE schema.
    Same output contract as map_record_to_lterlife() in lterlife_mapper.py:
      - exact coverage   → value as-is
      - partial coverage → value prefixed with '*'
      - no coverage      → '-'
    """
    # The Zenodo REST API nests descriptive fields under "metadata"
    # (metadata.description, metadata.creators, metadata.keywords, ...).
    # Lift that sub-object to the top level so the un-prefixed source keys
    # below resolve, while keeping the record-level keys (links, doi, ...).
    if isinstance(record.get("metadata"), dict):
        record = {**record, **record["metadata"]}

    extracted = _flatten(record)
    mapped = {}

    for lter_field, cfg in ZENODO_MAPPING.items():
        sources   = cfg["sources"]
        coverage  = cfg["coverage"]
        values: List[str] = []

        for src in sources:
            if src in extracted:
                for val in extracted[src]:
                    values.append(f"*{val}" if coverage == "partial" else val)

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