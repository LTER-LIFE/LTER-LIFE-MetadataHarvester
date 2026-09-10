"""
FastAPI Harvester UI + GeoNetwork importer (ZIP JSON-LD)
========================================================

What this service does
----------------------
1) /run
   - harvest records (OAI-PMH, etc.) using run_harvest(...)
   - export harvested XML records to JSON-LD files into ./jsonld/
   - create jsonld_records.zip (all harvested JSON-LD files)

2) /push-to-catalog
   - BEFORE importing, check GeoNetwork *catalogue* (NOT inside ZIP) to avoid duplicates
   - For each JSON-LD file in ./jsonld/:
       a) extract the dataset title from JSON-LD (IMPORTANT: your JSON uses "Title" with capital T)
       b) search GeoNetwork for that title
       c) if found -> SKIP that file
       d) if NOT found -> KEEP that file
   - create jsonld_records_filtered.zip from the kept files
   - upload the filtered ZIP to GeoNetwork
   - print detailed debug logs:
       - extracted title per file
       - exact query sent to GeoNetwork
       - GeoNetwork response status + snippet
       - decision: SKIP/PUSH

Notes
-----
- This version checks ONLY the catalogue; it does not dedupe inside the same harvest ZIP.
- Searching by TITLE can be imperfect. For best reliability, use DOI / Identifier later.
"""
from dotenv import load_dotenv
load_dotenv()
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

import os
import re
import json
import glob
from typing import Optional, Tuple, List

import requests

from harvesterv3_Frontend import run_harvest
from exporters.jsonld_exporter import export_jsonld_records, JSONLD_CONTEXT
from exporters.zip_utils import create_zip


# =====================================================
# GeoNetwork configuration
# =====================================================
GEONETWORK_BASE_URL = os.getenv(
    "GEONETWORK_BASE_URL",
    "https://lter-life-catalogue.qcdis.org/geonetwork"
)
GEONETWORK_PORTAL = os.getenv("GEONETWORK_PORTAL", "srv")
GEONETWORK_GROUP_ID = os.getenv("GEONETWORK_GROUP_ID", "7")


# =====================================================
# Persistent HTTP session (IMPORTANT for GeoNetwork)
# =====================================================
# We keep a single session so cookies (XSRF, sticky session) are preserved.
session = requests.Session()
session.verify = False  # keep same behavior as before (TLS not verified)


# =====================================================
# Keycloak configuration (Service account)
# =====================================================
KEYCLOAK_AUTH_SERVER_URL = os.getenv(
    "KEYCLOAK_AUTH_SERVER_URL",
    "https://lifewatch.lab.uvalight.net/auth"
)
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM", "vre")
KEYCLOAK_CLIENT_ID = os.getenv("KEYCLOAK_CLIENT_ID", "lter-life-catalogue-harvester")
KEYCLOAK_CLIENT_SECRET = os.getenv("KEYCLOAK_CLIENT_SECRET")

KEYCLOAK_USERNAME = os.getenv("KEYCLOAK_USERNAME", "harvester-service-account")
KEYCLOAK_PASSWORD = os.getenv("KEYCLOAK_PASSWORD")
if not KEYCLOAK_CLIENT_SECRET or not KEYCLOAK_PASSWORD:
    raise RuntimeError(
        "Missing Keycloak credentials. Set KEYCLOAK_CLIENT_SECRET and KEYCLOAK_PASSWORD as environment variables."
    )

# =====================================================
# App, templates, static files
# =====================================================
app = FastAPI()
templates = Jinja2Templates(directory="templates")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

STATIC_DIR = os.path.join(BASE_DIR, "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# =====================================================
# Local output folders/files
# =====================================================
JSONLD_DIR = os.path.join(BASE_DIR, "jsonld")  # exported JSON-LD files
ZIP_PATH = os.path.join(BASE_DIR, "jsonld_records.zip")  # full ZIP (all harvested)

FILTERED_DIR = os.path.join(BASE_DIR, "jsonld_filtered")  # filtered JSON-LD files
FILTERED_ZIP_PATH = os.path.join(BASE_DIR, "jsonld_records_filtered.zip")  # filtered ZIP


# =====================================================
# Auth helper: get Keycloak access token
# =====================================================
def get_keycloak_token() -> str:
    """
    Exchange service-account username/password for an access token.
    This token is used as: Authorization: Bearer <token>
    """
    token_url = (
        f"{KEYCLOAK_AUTH_SERVER_URL.rstrip('/')}"
        f"/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"
    )

    resp = requests.post(
        token_url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "password",
            "client_id": KEYCLOAK_CLIENT_ID,
            "client_secret": KEYCLOAK_CLIENT_SECRET,
            "username": KEYCLOAK_USERNAME,
            "password": KEYCLOAK_PASSWORD,
        },
        timeout=15,
        verify=False,
    )

    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail=f"Keycloak token request failed: {resp.text}")

    return resp.json()["access_token"]


# =====================================================
# GeoNetwork helpers
# =====================================================
def _gn_headers(access_token: str) -> dict:
    """
    GeoNetwork uses XSRF protection + sometimes sticky cookies.
    We first GET / to let GeoNetwork set cookies, then we return headers with:
      - Authorization: Bearer ...
      - X-XSRF-TOKEN (if cookie exists)
    """
    session.get(f"{GEONETWORK_BASE_URL}/", timeout=10, allow_redirects=True)
    xsrf = session.cookies.get("XSRF-TOKEN")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    if xsrf:
        headers["X-XSRF-TOKEN"] = xsrf
    return headers


def _norm_text(s: str) -> str:
    """Normalize user text (trim + collapse whitespace). Keep original casing unless needed."""
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def extract_title_from_jsonld(obj: dict) -> Optional[str]:
    """
    IMPORTANT for your JSON-LD shape:
      - Your record uses: "Title": [{"@value": "EMORID_tox riverine database"}]
      - DO NOT confuse it with "schema:name" (often a generic placeholder)
    This function tries "Title" first, then a few common fallbacks.
    """
    candidates = [
        "Title",  # <-- your exporter uses this (capital T)
        "http://purl.org/dc/terms/title",
        "dct:title",
        "dc:title",
        "dcterms:title",
        "title",
        "name",
        "schema:name",
    ]

    for k in candidates:
        v = obj.get(k)
        if v is None:
            continue

        # Case A: direct string
        if isinstance(v, str) and v.strip():
            return _norm_text(v)

        # Case B: dict with @value
        if isinstance(v, dict) and isinstance(v.get("@value"), str):
            return _norm_text(v["@value"])

        # Case C: list (often [{"@value": "..."}])
        if isinstance(v, list) and v:
            for it in v:
                if isinstance(it, str) and it.strip():
                    return _norm_text(it)
                if isinstance(it, dict) and isinstance(it.get("@value"), str):
                    return _norm_text(it["@value"])

    return None


def geonetwork_title_exists_debug(title_raw: str, access_token: str) -> bool:
    """
    Query GeoNetwork catalogue to check if a record exists for a given title.
    Prints:
      - extracted title
      - exact query string sent
      - response status + response snippet
      - number of hits
    """
    search_url = f"{GEONETWORK_BASE_URL}/{GEONETWORK_PORTAL}/api/search/records/_search"
    headers = _gn_headers(access_token)

    # Escape quotes for ES query_string
    q = title_raw.replace('"', '\\"')

    # We search using anytext/resourceTitle.
    # (If your GN index doesn't use resourceTitle for your schema, anytext usually still works.)
    query_string = f'(resourceTitle:"{q}" OR anytext:"{q}")'

    body = {
        "size": 5,
        "_source": {"includes": ["uuid", "id", "schema", "resourceTitle*", "title*", "anytext"]},
        "query": {
            "bool": {
                "must": [{"query_string": {"query": query_string}}],
                "filter": [{"term": {"isTemplate": {"value": "n"}}}],
            }
        },
    }

    print("\n==================== GN TITLE SEARCH ====================")
    print("title_raw:", repr(title_raw))
    print("url      :", search_url)
    print("query    :", query_string)
    print("body     :", json.dumps(body, ensure_ascii=False))
    resp = session.post(search_url, headers=headers, json=body, timeout=30)
    print("status   :", resp.status_code)
    print("resp     :", resp.text[:2000])
    print("=========================================================\n")

    if resp.status_code != 200:
        raise HTTPException(status_code=500, detail=f"GeoNetwork search failed: {resp.status_code} {resp.text}")

    data = resp.json()
    hits = (data.get("hits") or {}).get("hits") or []

    print(f"--> hits_count: {len(hits)}")
    for i, h in enumerate(hits[:5], start=1):
        src = h.get("_source", {})
        print(f"   hit[{i}] uuid={src.get('uuid')} id={src.get('id')} schema={src.get('schema')}")
        # Print available title fields, because GN setups differ
        for k in ["resourceTitle", "resourceTitleObject", "title", "titleObject"]:
            if k in src:
                print(f"      {k}: {src.get(k)}")
    print()

    return len(hits) > 0


def create_filtered_zip_skip_existing(access_token: str) -> Tuple[str, int, int]:
    """
    Build ./jsonld_records_filtered.zip by checking ONLY GeoNetwork catalogue:

      - For each JSON-LD file in ./jsonld/
        1) extract title (Title/@value)
        2) search GeoNetwork catalogue by title
        3) if exists -> SKIP
        4) else -> KEEP

    This function prints per-file decisions so you can debug duplicates.
    """
    os.makedirs(FILTERED_DIR, exist_ok=True)

    # Clean previous filtered directory
    for fp in glob.glob(os.path.join(FILTERED_DIR, "*")):
        try:
            os.remove(fp)
        except Exception:
            pass

    candidates = sorted(glob.glob(os.path.join(JSONLD_DIR, "*.json*")))
    kept = 0
    skipped = 0

    for fp in candidates:
        filename = os.path.basename(fp)

        # Read JSON-LD file
        try:
            with open(fp, "r", encoding="utf-8") as f:
                obj = json.load(f)
        except Exception as e:
            print(f"❌ SKIPPED (invalid JSON): {filename} error={e}")
            skipped += 1
            continue

        # Extract title used for dedupe
        title = extract_title_from_jsonld(obj)

        print("--------------------------------------------------")
        print(f"📄 FILE: {filename}")
        print(f"🏷 EXTRACTED TITLE: {repr(title)}")

        # If no title, we cannot search catalogue by title -> keep it
        if not title:
            print("⚠️  NO TITLE FOUND → KEEPING (cannot check catalogue by title)")
            out = os.path.join(FILTERED_DIR, filename)
            with open(out, "w", encoding="utf-8") as fo:
                json.dump(obj, fo, ensure_ascii=False, indent=2)
            kept += 1
            continue

        # Catalogue check (GeoNetwork)
        exists = geonetwork_title_exists_debug(title, access_token)
        if exists:
            print("❌ DECISION: SKIP (already exists in GeoNetwork catalogue)")
            skipped += 1
            continue

        print("✅ DECISION: KEEP (not found in catalogue) -> will be pushed")
        out = os.path.join(FILTERED_DIR, filename)
        with open(out, "w", encoding="utf-8") as fo:
            json.dump(obj, fo, ensure_ascii=False, indent=2)
        kept += 1

    # Zip the kept files
    create_zip(FILTERED_DIR, FILTERED_ZIP_PATH)
    print(f"\nFILTER RESULT: kept={kept}, skipped_existing_in_catalogue={skipped}\n")

    return FILTERED_ZIP_PATH, kept, skipped


# =====================================================
# UI: Home page
# =====================================================
@app.get("/", response_class=HTMLResponse)
def ui_form(request: Request):
    """Serve the HTML form UI."""
    return templates.TemplateResponse("form.html", {"request": request})


# =====================================================
# Harvest + export
# =====================================================
@app.post("/run", response_class=HTMLResponse)
def run_harvest_ui(
    request: Request,
    node_name: str = Form(...),
    url: str = Form(...),
    from_date: str = Form(None),
    until_date: str = Form(None),
    export_format: str = Form("jsonld"),
):
    """
    Run harvest:
      - run_harvest(...) returns harvested XML records + UI records
      - export harvested XML -> JSON-LD files under ./jsonld/
      - create jsonld_records.zip from ./jsonld/
    """
    result = run_harvest(
        portal_url=url,
        start_date=from_date or None,
        end_date=until_date or None,
    )

    xml_records = result.get("xml_records", [])
    ui_records = result.get("ui_records", [])

    # Just for UI display
    record_count = (
        sum(1 for r in ui_records if isinstance(r, dict) and r.get("separator")) + 1
        if ui_records else 0
    )

    # Export JSON-LD and zip it
    #if export_format == "jsonld" and xml_records:
     #   os.makedirs(JSONLD_DIR, exist_ok=True)
      #  export_jsonld_records(records=xml_records, output_dir=JSONLD_DIR, context=JSONLD_CONTEXT)
      #  create_zip(JSONLD_DIR, ZIP_PATH)

    return templates.TemplateResponse(
        "form.html",
        {
            "request": request,
            "node_name": node_name,
            "url": url,
            "from_date": from_date,
            "until_date": until_date,
            "records": ui_records,
            "record_count": record_count,
            "export_format": export_format,
        },
    )


# =====================================================
# Download: original ZIP (all harvested records)
# =====================================================
@app.get("/download/jsonld")
def download_jsonld_zip():
    """Download jsonld_records.zip (all harvested records)."""
    if not os.path.exists(ZIP_PATH):
        raise HTTPException(status_code=404, detail="ZIP file not found. Run harvest first.")
    return FileResponse(ZIP_PATH, media_type="application/zip", filename="lterlife_jsonld_records.zip")


# =====================================================
# Download: filtered ZIP (only non-duplicates)
# =====================================================
@app.get("/download/jsonld-filtered")
def download_jsonld_filtered_zip():
    """Download jsonld_records_filtered.zip (after catalogue-dedupe)."""
    if not os.path.exists(FILTERED_ZIP_PATH):
        raise HTTPException(status_code=404, detail="Filtered ZIP not found. Push once to generate it.")
    return FileResponse(
        FILTERED_ZIP_PATH,
        media_type="application/zip",
        filename="lterlife_jsonld_records_filtered.zip",
    )


# =====================================================
# Push to GeoNetwork: dedupe-by-title against catalogue
# =====================================================

@app.post("/push-to-catalog", response_class=HTMLResponse)
def push_to_catalog(request: Request):
    """
    Push records to GeoNetwork WITHOUT creating duplicates (by title):

    Steps:
      1) Ensure jsonld_records.zip exists (created by /run)
      2) Get Keycloak token
      3) Build filtered zip by checking GeoNetwork catalogue for each record title
      4) Upload filtered zip to GeoNetwork /srv/api/records
    """
    if not os.path.exists(ZIP_PATH):
        raise HTTPException(status_code=400, detail="ZIP not found. Run harvest first.")

    # 1) Get auth token
    access_token = get_keycloak_token()

    # 2) Create filtered ZIP (catalogue-only check)
    filtered_zip, kept, skipped = create_filtered_zip_skip_existing(access_token)

    if kept == 0:
        return templates.TemplateResponse(
            "form.html",
            {
                "request": request,
                "push_report": {
                    "message": "Nothing to push: all harvested records already exist in the catalogue (by title).",
                    "kept": kept,
                    "skipped": skipped,
                },
            },
        )

    # 3) Upload FILTERED records in small ZIP batches (avoid nginx 504 timeouts)
    import math
    import zipfile
    import tempfile

    BATCH_SIZE = int(os.getenv("GN_IMPORT_BATCH_SIZE", "15"))  # try 10–20
    import_url = f"{GEONETWORK_BASE_URL}/{GEONETWORK_PORTAL}/api/records"
    headers = _gn_headers(access_token)

    # Collect the *kept* JSON-LD files (these are what we want to import)
    kept_files = sorted(glob.glob(os.path.join(FILTERED_DIR, "*.json*")))

    if not kept_files:
        report = {
            "message": "Nothing to push: filtered set is empty.",
            "dedupe": {"kept": kept, "skipped": skipped, "mode": "skip-if-title-exists"},
        }
        return templates.TemplateResponse("form.html", {"request": request, "push_report": report})

    def _chunked(lst, n):
        for i in range(0, len(lst), n):
            yield lst[i:i + n]

    batch_reports = []
    ok_batches = 0
    failed_batches = 0
    total_batches = math.ceil(len(kept_files) / BATCH_SIZE)

    with tempfile.TemporaryDirectory() as tmpdir:
        for bidx, chunk in enumerate(_chunked(kept_files, BATCH_SIZE), start=1):
            batch_zip = os.path.join(tmpdir, f"jsonld_records_filtered_batch_{bidx}.zip")

            # Create a zip containing only this chunk (store basenames)
            with zipfile.ZipFile(batch_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for fp in chunk:
                    zf.write(fp, arcname=os.path.basename(fp))

            # Upload this batch
            with open(batch_zip, "rb") as f:
                resp = session.post(
                    import_url,
                    files={"file": (os.path.basename(batch_zip), f, "application/zip")},
                    data={
                        "metadataType": "METADATA",
                        "uuidProcessing": "GENERATEUUID",
                        "group": GEONETWORK_GROUP_ID,
                        "rejectIfInvalid": "false",
                        "publishToAll": "false",
                        "assignToCatalog": "true",
                        "allowEditGroupMembers": "true",
                        "transformWith": "_none_",
                    },
                    headers=headers,
                    timeout=300,
                )

            # Debug per-batch result
            print(f"=== ZIP IMPORT DEBUG (BATCH {bidx}/{total_batches}) ===")
            print("batch_size:", len(chunk))
            print("kept_total:", kept, "skipped_total:", skipped)
            print("status:", resp.status_code)
            print("content-type:", resp.headers.get("content-type"))
            print("body:", resp.text[:4000])
            print("===============================================")

            if resp.status_code in (200, 201):
                ok_batches += 1
                try:
                    batch_reports.append({"batch": bidx, "status": resp.status_code, "report": resp.json()})
                except Exception:
                    batch_reports.append({"batch": bidx, "status": resp.status_code, "report": resp.text[:2000]})
            else:
                failed_batches += 1
                batch_reports.append({"batch": bidx, "status": resp.status_code, "error": resp.text[:2000]})

    # Build final report for UI
    report = {
        "message": "Batch import finished.",
        "batches_total": total_batches,
        "batches_ok": ok_batches,
        "batches_failed": failed_batches,
        "dedupe": {"kept": kept, "skipped": skipped, "mode": "skip-if-title-exists"},
        # keep UI small; logs still have full details
        "batch_reports_preview": batch_reports[:10],
    }

    # If everything failed, raise a 500 so the UI shows "error"
    if ok_batches == 0 and failed_batches > 0:
        first = batch_reports[0]
        raise HTTPException(
            status_code=500,
            detail=f"GeoNetwork batch ZIP import failed. First batch error: {first}",
        )

    return templates.TemplateResponse("form.html", {"request": request, "push_report": report})