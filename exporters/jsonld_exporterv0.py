import os
import json
import hashlib
import shutil
from datetime import datetime
from typing import Dict, Any, List, Optional

from converters.lterlife_mapper import map_record_to_lterlife


JSONLD_CONTEXT: Dict[str, Any] = {
    "@context": {
        "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
        "xsd": "http://www.w3.org/2001/XMLSchema#",
        "pav": "http://purl.org/pav/",
        "schema": "http://schema.org/",
        "oslc": "http://open-services.net/ns/core#",
        "skos": "http://www.w3.org/2004/02/skos/core#",

        "Title": "http://purl.org/dc/terms/title",
        "Description": "http://purl.org/dc/terms/description",
        "Spatial Coverage": "http://purl.org/dc/terms/spatial",
        "Creator": "http://purl.org/dc/terms/creator",
        "Publisher": "http://purl.org/dc/terms/publisher",
        "Identifier": "http://purl.org/dc/terms/identifier",
        "keyword": "http://www.w3.org/ns/dcat#keyword",
        "landing page": "http://www.w3.org/ns/dcat#landingPage",
        "access URL": "http://www.w3.org/ns/dcat#accessURL",
        "Temporal Coverage": "http://purl.org/dc/terms/temporal",
        "Access Rights": "http://purl.org/dc/terms/accessRights",
        "language": "http://loki.cae.drexel.edu/~wbs/ontology/2004/09/iso-19115#metadataLanguage",
    }
}


def _as_list(value: Any) -> List[str]:
    """Normalize mapper output to a list of strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    return [str(value)]


def pick_stable_id(lterlife_record: Dict[str, Any]) -> Optional[str]:
    """
    Pick a stable identifier for the record.
    Priority:
      1) A DOI URL (contains 'doi.org/')
      2) Any non-empty Identifier value
      3) Any non-empty Landing page value
    Returns None if nothing usable exists.
    """
    candidates: List[str] = []

    # Identifier field
    if "Identifier" in lterlife_record:
        candidates += _as_list(lterlife_record["Identifier"].get("value"))

    # Landing page fallback (note: your key is 'Landing page' with capital L in output)
    if "Landing page" in lterlife_record:
        candidates += _as_list(lterlife_record["Landing page"].get("value"))

    # Prefer DOI URL form if present
    for c in candidates:
        c = c.strip()
        if c and c != "-" and "doi.org/" in c:
            return c

    # Otherwise first usable candidate
    for c in candidates:
        c = c.strip()
        if c and c != "-":
            return c

    return None


def stable_filename(stable_id: str) -> str:
    """
    Create a deterministic short filename from a stable identifier.
    Keeps filenames consistent across runs even if record order changes.
    """
    return hashlib.sha1(stable_id.encode("utf-8")).hexdigest()[:16]


from typing import Union  # add at top with other imports

def export_jsonld_records(
    records: List[Union[str, Dict[str, Any]]],   # ✅ accept XML or mapped dict
    output_dir: str,
    context: Dict[str, Any],
) -> None:
    """
    Export one JSON-LD file per harvested record.

    records can be:
      - str: raw harvested XML
      - dict: already-mapped (and possibly LLM-enriched) LTER-LIFE record
    """

    if "@context" not in context:
        raise ValueError("JSON-LD context must contain '@context'")

    # Clean output_dir so old JSON-LD files don't remain and get zipped
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    for idx, rec in enumerate(records, start=1):

        # ✅ If already mapped/enriched, use it directly
        if isinstance(rec, dict):
            lterlife_record = rec
        else:
            # ✅ Backward compatible: XML -> map
            lterlife_record = map_record_to_lterlife(rec)

        stable_id = pick_stable_id(lterlife_record)

        record_file_id = stable_filename(stable_id) if stable_id else f"record_{idx:03d}"
        print(f"\n===== EXPORT DEBUG record {idx} =====", flush=True)
        print("rec type:", type(rec).__name__, flush=True)
        print("has Spatial coverage before build:", isinstance(rec, dict) and "Spatial coverage" in rec, flush=True)

        if isinstance(rec, dict):
            print("Spatial coverage payload before build:", rec.get("Spatial coverage"), flush=True)
            print("LLM_enrichment_source_url before build:", rec.get("LLM_enrichment_source_url"), flush=True)
        jsonld = build_jsonld_record(
            lterlife_record=lterlife_record,
            context=context,
            fallback_record_id=record_file_id,
        )
        print("jsonld has Spatial coverage:", "Spatial coverage" in jsonld, flush=True)
        print("jsonld Spatial coverage value:", jsonld.get("Spatial coverage"), flush=True)
        print("jsonld has LLM_enrichment_source_url:", "LLM_enrichment_source_url" in jsonld, flush=True)
        path = os.path.join(output_dir, f"{record_file_id}.jsonld")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(jsonld, f, indent=2, ensure_ascii=False)
        # DEBUG: read back the file we just wrote
        with open(path, "r", encoding="utf-8") as f:
            written = f.read()

        print("written file contains 'Spatial coverage':", "Spatial coverage" in written, flush=True)
        print("written file contains 'LLM_enrichment_source_url':", "LLM_enrichment_source_url" in written,
              flush=True)
        print("written path:", path, flush=True)


def build_jsonld_record(
    lterlife_record: Dict[str, Any],
    context: Dict[str, Any],
    fallback_record_id: str,
) -> Dict[str, Any]:
    """
    Convert one LTER-LIFE mapped record into a JSON-LD document.

    '@id' is set to a stable identifier (DOI/URL) when available;
    otherwise it falls back to the generated local record id.
    """

    jsonld: Dict[str, Any] = dict(context)

    stable_id = pick_stable_id(lterlife_record)
    jsonld["@id"] = stable_id if stable_id else fallback_record_id

    for lter_field, payload in lterlife_record.items():
        value = payload.get("value")

        # omit explicit no-coverage fields
        if value == "-" or value is None:
            continue

        if isinstance(value, list):
            jsonld[lter_field] = [{"@value": v} for v in value if v != "-" and v is not None]
        else:
            jsonld[lter_field] = [{"@value": value}]

    # provenance metadata
    jsonld["pav:createdOn"] = datetime.utcnow().isoformat()
    jsonld["schema:name"] = "LTER-LIFE metadata record"

    return jsonld