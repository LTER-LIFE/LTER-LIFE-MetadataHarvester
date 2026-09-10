import os
import json
import hashlib
import shutil
from datetime import datetime
from typing import Dict, Any, List, Optional


JSONLD_CONTEXT: Dict[str, Any] = {
    "@context": {
        "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
        "xsd": "http://www.w3.org/2001/XMLSchema#",
        "pav": "http://purl.org/pav/",
        "schema": "http://schema.org/",
        "oslc": "http://open-services.net/ns/core#",
        "skos": "http://www.w3.org/2004/02/skos/core#",

        # Match actual LTER-LIFE field names exactly
        "Title": "http://purl.org/dc/terms/title",
        "Description": "http://purl.org/dc/terms/description",
        "Spatial coverage": "http://purl.org/dc/terms/spatial",
        "Creator": "http://purl.org/dc/terms/creator",
        "Publisher": "http://purl.org/dc/terms/publisher",
        "Identifier": "http://purl.org/dc/terms/identifier",
        "Keyword": "http://www.w3.org/ns/dcat#keyword",
        "Landing page": "http://www.w3.org/ns/dcat#landingPage",
        "Access URL": "http://www.w3.org/ns/dcat#accessURL",
        "Temporal coverage": "http://purl.org/dc/terms/temporal",
        "Access rights": "http://purl.org/dc/terms/accessRights",
        "Language (metadata)": "http://loki.cae.drexel.edu/~wbs/ontology/2004/09/iso-19115#metadataLanguage",
    }
}


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    return [str(value)]


def pick_stable_id(lterlife_record: Dict[str, Any]) -> Optional[str]:
    """
    Pick a stable identifier for the record.
    Priority:
      1) DOI URL
      2) any non-empty Identifier
      3) any non-empty Landing page
    """
    candidates: List[str] = []

    if "Identifier" in lterlife_record and isinstance(lterlife_record["Identifier"], dict):
        candidates += _as_list(lterlife_record["Identifier"].get("value"))

    if "Landing page" in lterlife_record and isinstance(lterlife_record["Landing page"], dict):
        candidates += _as_list(lterlife_record["Landing page"].get("value"))

    for c in candidates:
        c = c.strip()
        if c and c != "-" and "doi.org/" in c:
            return c

    for c in candidates:
        c = c.strip()
        if c and c != "-":
            return c

    return None


def stable_filename(stable_id: str) -> str:
    return hashlib.sha1(stable_id.encode("utf-8")).hexdigest()[:16]


def export_jsonld_records(
    records: List[Dict[str, Any]],
    output_dir: str,
    context: Dict[str, Any],
) -> None:
    """
    Export one JSON-LD file per harvested record.

    IMPORTANT:
    - This exporter now accepts only already-mapped / already-enriched
      LTER-LIFE records (dicts).
    - Do NOT pass raw XML strings here.
    """

    if "@context" not in context:
        raise ValueError("JSON-LD context must contain '@context'")

    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    for idx, rec in enumerate(records, start=1):
        if not isinstance(rec, dict):
            raise TypeError(
                f"export_jsonld_records expects mapped dict records only, "
                f"but got {type(rec).__name__} at position {idx}"
            )

        lterlife_record = rec
        stable_id = pick_stable_id(lterlife_record)
        record_file_id = stable_filename(stable_id) if stable_id else f"record_{idx:03d}"

        jsonld = build_jsonld_record(
            lterlife_record=lterlife_record,
            context=context,
            fallback_record_id=record_file_id,
        )

        path = os.path.join(output_dir, f"{record_file_id}.jsonld")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(jsonld, f, indent=2, ensure_ascii=False)

        print(f"✅ Wrote JSON-LD: {path}", flush=True)


def build_jsonld_record(
    lterlife_record: Dict[str, Any],
    context: Dict[str, Any],
    fallback_record_id: str,
) -> Dict[str, Any]:
    """
    Convert one LTER-LIFE mapped record into a JSON-LD document.
    """

    jsonld: Dict[str, Any] = dict(context)

    stable_id = pick_stable_id(lterlife_record)
    jsonld["@id"] = stable_id if stable_id else fallback_record_id

    for lter_field, payload in lterlife_record.items():
        if not isinstance(payload, dict):
            continue

        value = payload.get("value")

        # Skip missing values
        if value == "-" or value is None:
            continue

        if isinstance(value, list):
            cleaned = [v for v in value if v != "-" and v is not None]
            if cleaned:
                jsonld[lter_field] = [{"@value": str(v)} for v in cleaned]
        else:
            jsonld[lter_field] = [{"@value": str(value)}]

    jsonld["pav:createdOn"] = datetime.utcnow().isoformat()
    jsonld["schema:name"] = "LTER-LIFE metadata record"

    return jsonld