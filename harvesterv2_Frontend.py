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

def run_harvest(portal_url: str, start_date=None, end_date=None, max_records=None):
    """
    max_records:
      - None  => harvest ALL records (requires harvest_* functions to paginate)
      - >0    => harvest up to that many records
      - 0/<0  => treated as ALL (converted to None)
    """
    proto, api = detect_protocol(portal_url)

    print(f"➡️ Detected protocol: {proto}")
    print(f"🔗 API endpoint: {api}")

    # Optional safety cap to avoid huge accidental harvests.
    # Set to None if you want truly unlimited when max_records=None.
    HARD_CAP = None  # e.g., 5000 to protect your service
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
    # ✅ SCHEMA DETECTION + MAPPING (UI table rows)
    # =============================
    output_records = []
    for idx, record_xml in enumerate(records):
        mapped_fields = map_record_to_lterlife(record_xml)

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
    base_dir = os.path.dirname(os.path.abspath(__file__))
    jsonld_dir = os.path.join(base_dir, "jsonld")
    zip_path = os.path.join(base_dir, "jsonld_records.zip")

    export_jsonld_records(
        records=records,
        output_dir=jsonld_dir,
        context=JSONLD_CONTEXT,
    )
    create_zip(jsonld_dir, zip_path)

    return {
        "xml_records": records,        # raw XML
        "ui_records": output_records,  # table rows
    }

if __name__ == "__main__":
    print("Run through FastAPI UI only.")