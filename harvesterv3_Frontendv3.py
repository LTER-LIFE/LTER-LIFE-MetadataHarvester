import os
import requests
import xml.etree.ElementTree as ET
from collections import defaultdict
from urllib.parse import urlparse

from protocols.oai import harvest_oai
from protocols.csw import harvest_csw
from protocols.stac import harvest_stac
from protocols.geonetwork import harvest_geonetwork
from protocols.odata import harvest_odata

from converters.lterlife_mapper import map_record_to_lterlife
from exporters.jsonld_exporter import export_jsonld_records, JSONLD_CONTEXT
from exporters.zip_utils import create_zip
from llm_harvester_client import enrich_url, LLMHarvesterError

requests.packages.urllib3.disable_warnings()

# =====================================================
# ✅ KNOWN API MAPPINGS
# =====================================================
KNOWN_APIS = {
    "stac.ecodatacube.eu": ("STAC", "https://stac.ecodatacube.eu/api/stac"),
    "data.rivm.nl": ("GeoNetwork", "https://data.rivm.nl/meta/srv/api/records"),
    "dataverse.nioz.nl": ("OAI-PMH", "https://dataverse.nioz.nl/oai"),
    "dataverse.nl": ("OAI-PMH", "https://dataverse.nl/oai"),
    "api.gbif.org": ("OAI-PMH", "https://api.gbif.org/v1/oai-pmh/registry"),
    "nationaalgeoregister.nl": ("CSW", "https://nationaalgeoregister.nl/geonetwork/srv/eng/csw"),
    "opendata.cbs.nl": ("OData", "https://opendata.cbs.nl/ODataApi/OData/82070NED"),
}

# =====================================================
# ✅ PROTOCOL DETECTION
# =====================================================
def normalize_base(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"

def detect_protocol(url: str):
    base = normalize_base(url)
    host = urlparse(base).netloc
    return KNOWN_APIS.get(host, ("Unknown", base))

# =====================================================
# ✅ Helper
# =====================================================
def guess_landing_url_from_xml(xml_string: str) -> str | None:
    """
    Best-effort: find any URL-looking text in the XML (identifier, relation, landingPage).
    You can later make this smarter per protocol/schema.
    """
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

    # Prefer DOI resolvers or obvious landing pages if present
    for u in urls:
        if "doi.org" in u:
            return u
    return urls[0] if urls else None


def extract_raw_fields(xml_string: str) -> dict:
    """
    Returns:
      { "title": [...], "creator": [...], ... }  (namespace-stripped tags)
    """
    root = ET.fromstring(xml_string)
    fields = defaultdict(list)
    for elem in root.iter():
        if elem.text and elem.text.strip():
            tag = elem.tag.split("}")[-1]  # strip namespace
            fields[tag].append(elem.text.strip())
    return fields

def _resolve_effective_limit(max_records, hard_cap: int | None):
    """
    max_records:
      - None => harvest all (or up to hard_cap if provided)
      - int  => harvest up to that number
    """
    if max_records is None:
        return hard_cap  # can still be None (meaning truly unlimited)
    try:
        m = int(max_records)
    except Exception:
        raise ValueError("max_records must be an int or None")
    return None if m <= 0 else m

# =====================================================
# ✅ DISPATCHER (MAIN ENTRY POINT)
# =====================================================
def _is_missing_lter_value(v) -> bool:
    """
    Your mapper uses '-' for missing.
    Also treat empty / n/a / none as missing.
    Works for strings and lists.
    """
    if v is None:
        return True
    if isinstance(v, list):
        # list is missing if all elements are missing-ish
        return all(_is_missing_lter_value(x) for x in v)
    s = str(v).strip().lower()
    return s in {"", "-", "n/a", "none", "null", "unknown"}

def _get_first_url_from_mapped(mapped_fields: dict) -> str | None:
    """
    Prefer the LTER field 'Landing page' if it exists and looks like a URL.
    Your mapper may prefix partial values with '*', so we strip leading '*'.
    """
    landing_payload = mapped_fields.get("Landing page", {})
    v = landing_payload.get("value")

    def norm(u: str) -> str:
        u = (u or "").strip()
        if u.startswith("*"):
            u = u[1:].strip()
        return u

    if isinstance(v, list):
        for it in v:
            u = norm(it)
            if u.startswith("http://") or u.startswith("https://"):
                return u
    else:
        u = norm(v)
        if u.startswith("http://") or u.startswith("https://"):
            return u

    return None

def _normalize_llm_value(x):
    """Return a string or list[str] from LLM output values."""
    if x is None:
        return None
    if isinstance(x, list):
        return [str(v).strip() for v in x if v is not None and str(v).strip()]
    s = str(x).strip()
    return s if s else None


def apply_llm_res_to_lterlife(mapped_fields: dict, llm_res: dict) -> None:
    """
    Take llm_res keys like: Dataset / Geographic Location / Organization / Person
    and fill missing LTER-LIFE fields, marking with ** and printing what was filled.
    """

    # Map LLM keys -> LTER fields (best-effort)
    # NOTE: adjust these mappings once you see what the LLM actually returns for NIOZ pages
    mapping = {
        "Dataset": ["Title", "Description", "Identifier"],  # best-effort: might contain title/summary
        "Geographic Location": ["Spatial coverage"],
        "Organization": ["Publisher", "Responsible party", "Contact point"],
        "Person": ["Creator", "Responsible party", "Contact point"],
    }

    for llm_key, target_fields in mapping.items():
        if llm_key not in llm_res:
            continue

        raw_val = _normalize_llm_value(llm_res.get(llm_key))
        if raw_val is None:
            continue

        for field in target_fields:
            if field not in mapped_fields:
                continue
            if not _is_missing_lter_value(mapped_fields[field].get("value")):
                continue  # do not overwrite existing metadata

            # mark with ** and write
            if isinstance(raw_val, list):
                val = [f"**{v}" for v in raw_val]
            else:
                val = f"**{raw_val}"

            mapped_fields[field]["value"] = val
            mapped_fields[field]["raw_fields"] = ["LLM-harvester-service"]

            print(f"🧠 LLM filled '{field}' from '{llm_key}': {raw_val}", flush=True)

def debug_print_raw_fields(record_xml: str, record_idx: int):
    try:
        raw_fields = extract_raw_fields(record_xml)
        print(f"\n===== DEBUG RAW FIELDS for record #{record_idx} =====", flush=True)
        print("Raw XML field names:", sorted(raw_fields.keys()), flush=True)

        for k in sorted(raw_fields.keys()):
            vals = raw_fields[k]
            preview = vals[:2]  # only first 2 values to avoid huge logs
            print(f"  - {k}: {preview}", flush=True)
    except Exception as e:
        print(f"⚠️ Failed to print raw fields for record #{record_idx}: {e}", flush=True)


def debug_print_mapped_fields(mapped_fields: dict, record_idx: int):
    print(f"\n===== DEBUG MAPPED LTER FIELDS for record #{record_idx} =====", flush=True)
    for field, payload in mapped_fields.items():
        raw_fields = payload.get("raw_fields", [])
        value = payload.get("value")
        print(
            f"  - {field} | raw_fields={raw_fields} | value={value}",
            flush=True
        )


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

def apply_llm_res_to_lterlife(mapped_fields: dict, llm_res: dict) -> None:
    """
    Take llm_res keys like: Dataset / Geographic Location / Organization / Person
    and fill missing LTER-LIFE fields, marking with ** and printing what was filled.
    """

    mapping = {
        "Dataset": ["Title", "Description", "Identifier"],
        "Geographic Location": ["Spatial coverage"],
        "Organization": ["Publisher", "Responsible party", "Contact point"],
        "Person": ["Creator", "Responsible party", "Contact point"],
    }

    print("\n===== DEBUG LLM -> LTER MAPPING START =====", flush=True)
    print("Available LLM keys:", sorted(llm_res.keys()), flush=True)
    print("Mapping rules:", mapping, flush=True)

    used_llm_keys = set()

    for llm_key, target_fields in mapping.items():
        print(f"\nChecking LLM key '{llm_key}'...", flush=True)

        if llm_key not in llm_res:
            print(f"  -> NOT FOUND in LLM response", flush=True)
            continue

        used_llm_keys.add(llm_key)

        raw_val = _normalize_llm_value(llm_res.get(llm_key))
        print(f"  -> Found in LLM response, raw value = {llm_res.get(llm_key)}", flush=True)
        print(f"  -> Normalized value = {raw_val}", flush=True)

        if raw_val is None:
            print("  -> Skipped because normalized value is empty", flush=True)
            continue

        for field in target_fields:
            print(f"    Target LTER field: '{field}'", flush=True)

            if field not in mapped_fields:
                print("      -> Target field not present in mapped_fields", flush=True)
                continue

            current_value = mapped_fields[field].get("value")
            print(f"      -> Current value before mapping: {current_value}", flush=True)

            if not _is_missing_lter_value(current_value):
                print("      -> Skipped because target field already has value", flush=True)
                continue

            if isinstance(raw_val, list):
                val = [f"**{v}" for v in raw_val]
            else:
                val = f"**{raw_val}"

            mapped_fields[field]["value"] = val
            mapped_fields[field]["raw_fields"] = ["LLM-harvester-service"]

            print(f"      -> ✅ FILLED '{field}' from '{llm_key}' with {val}", flush=True)

    unused_llm_keys = sorted(set(llm_res.keys()) - used_llm_keys)
    print("\nUnused LLM keys (returned by LLM but not covered by mapping):", unused_llm_keys, flush=True)
    print("===== DEBUG LLM -> LTER MAPPING END =====\n", flush=True)

def run_harvest(portal_url: str, start_date=None, end_date=None, max_records=None):
    proto, api = detect_protocol(portal_url)
    print(f"➡️ Detected protocol: {proto}")
    print(f"🔗 API endpoint: {api}")

    HARD_CAP = None
    effective_limit = _resolve_effective_limit(max_records, HARD_CAP)

    # =============================
    # ✅ HARVEST PHASE
    # =============================
    if proto == "OAI-PMH":
        records = harvest_oai(api, effective_limit, start_date, end_date)
    elif proto == "CSW":
        records = harvest_csw(api, effective_limit)
    elif proto == "STAC":
        records = harvest_stac(api, effective_limit)
    elif proto == "GeoNetwork":
        records = harvest_geonetwork(api, effective_limit)
    elif proto == "OData":
        records = harvest_odata(api, effective_limit)
    else:
        print("⚠️ Unsupported protocol")
        return {"xml_records": [], "ui_records": []}

    print(f"✅ Harvested records: {len(records)}")

    # =============================
    # ✅ MAPPING + (optional) LLM enrichment
    # =============================
    mapped_records_for_export = []
    output_records = []
    important_fields = None  # will be set after first mapping

    for idx, record_xml in enumerate(records):
        mapped_fields = map_record_to_lterlife(record_xml)
        #debug_print_raw_fields(record_xml, idx + 1)
        #debug_print_mapped_fields(mapped_fields, idx + 1)
        missing_fields = debug_print_missing_fields(mapped_fields, idx + 1)

        # Define "all fields" once (keys are stable across records)
        if important_fields is None:
            important_fields = list(mapped_fields.keys())

        # Trigger LLM if ANY field is missing
        any_missing = any(
            _is_missing_lter_value(mapped_fields.get(f, {}).get("value"))
            for f in important_fields
        )

        if any_missing:
            landing_url = _get_first_url_from_mapped(mapped_fields)

            # fallback if mapped landing page isn't a URL
            if not landing_url:
                landing_url = guess_landing_url_from_xml(record_xml)

            if landing_url:
                try:
                    #print(f"🤖 LLM enrichment triggered for record #{idx + 1} url={landing_url}", flush=True)
                    #print(f"\n===== DEBUG LLM INPUT for record #{idx + 1} =====", flush=True)
                    #print(f"Landing URL sent to LLM: {landing_url}", flush=True)
                    #print(f"Missing fields that triggered LLM: {missing_fields}", flush=True)

                    llm = enrich_url(landing_url)
                    #print("✅ enrich_url() returned", flush=True)

                    llm_res = llm.get("result", {})
                    debug_print_llm_response(llm_res, idx + 1)
                    #print("🔎 Harvester expects fields:", important_fields, flush=True)
                    print("🔎 LLM returned keys:", sorted(list(llm_res.keys())), flush=True)
                    #print("🔎 Harvester expects fields:", important_fields, flush=True)

                    # ✅ APPLY MAPPING / FILLING HERE (must be INSIDE try)
                    apply_llm_res_to_lterlife(mapped_fields, llm_res)
                    #print(f"\n===== DEBUG FINAL ENRICHED RECORD #{idx + 1} =====", flush=True)
                    #for field, payload in mapped_fields.items():
                     #   print(
                     #       f"  - {field} | raw_fields={payload.get('raw_fields', [])} | value={payload.get('value')}",
                      #      flush=True
                      #  )

                    mapped_fields["LLM_enrichment_source_url"] = {
                        "value": landing_url,
                        "raw_fields": ["LLM-harvester-service"],
                    }

                except Exception as e:
                    import traceback
                    print("❌ LLM enrichment FAILED with exception:", repr(e), flush=True)
                    traceback.print_exc()

                    mapped_fields["LLM_enrichment_error"] = {
                        "value": f"{type(e).__name__}: {e}",
                        "raw_fields": ["LLM-harvester-service"],
                    }

        # UI rows
        mapped_records_for_export.append(mapped_fields)
        for lter_field, payload in mapped_fields.items():
            output_records.append({
                "lterlife_field": lter_field,
                "raw_field": ", ".join(payload.get("raw_fields", [])),
                "value": payload.get("value", ""),
            })

        if idx < len(records) - 1:
            output_records.append({"separator": True})

    # =============================
    # ✅ Export phase (ONCE)
    # =============================
    print("\n===== DEBUG RECORDS SENT TO EXPORT =====", flush=True)
    for i, rec in enumerate(mapped_records_for_export, start=1):
        llm_derived = []
        for field, payload in rec.items():
            if "LLM-harvester-service" in payload.get("raw_fields", []):
                llm_derived.append((field, payload.get("value")))
        #print(f"Record #{i} LLM-derived fields: {llm_derived}", flush=True)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    jsonld_dir = os.path.join(base_dir, "jsonld")
    zip_path = os.path.join(base_dir, "jsonld_records.zip")

    export_jsonld_records(records=mapped_records_for_export, output_dir=jsonld_dir, context=JSONLD_CONTEXT)
    create_zip(jsonld_dir, zip_path)
    return {"xml_records": records, "ui_records": output_records}

if __name__ == "__main__":
    print("Run through FastAPI UI only.")