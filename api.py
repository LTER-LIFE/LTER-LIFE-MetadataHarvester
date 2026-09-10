"""
FastAPI Harvester UI + GeoNetwork importer (ZIP JSON-LD)
========================================================

This version supports:
1) /run
   - starts a background harvest job
   - returns JSON: { "job_id": "..." }

2) /run-status/{job_id}
   - returns live job status + logs + phases + final result

3) /enrich-llm/{job_id}
   - runs optional LLM enrichment on an already-harvested job's mapped records
   - returns the updated job status/result

4) /push-to-catalog
   - pushes the latest filtered ZIP to GeoNetwork
   - returns JSON with the import report

Phase model used by the frontend
--------------------------------
Each job contains a "phases" array like:

[
  {
    "key": "phase1",
    "title": "PHASE 1 — Endpoint Harvesting",
    "status": "DONE",
    "messages": [
      "Connecting to source endpoint.",
      "Total OAI records harvested: 5"
    ]
  },
  ...
]

Allowed phase statuses:
- WAITING
- RUNNING
- DONE
- ERROR
"""

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

import os
import re
import json
import glob
import math
import uuid
import zipfile
import tempfile
import threading
from copy import deepcopy
from typing import Optional, Tuple, List, Dict, Any

import requests

from harvesterv3_Frontend import run_harvest, enrich_and_reexport
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
# Persistent HTTP session
# =====================================================
session = requests.Session()
session.verify = False


# =====================================================
# Keycloak configuration
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
JSONLD_DIR = os.path.join(BASE_DIR, "jsonld")
ZIP_PATH = os.path.join(BASE_DIR, "jsonld_records.zip")

FILTERED_DIR = os.path.join(BASE_DIR, "jsonld_filtered")
FILTERED_ZIP_PATH = os.path.join(BASE_DIR, "jsonld_records_filtered.zip")


# =====================================================
# Supported URLs for mapping
# =====================================================
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

# Explicit source ids offered by the UI dropdown (mirrors
# harvesterv3_Frontend.SOURCES); when one of these is chosen we trust it
# and skip the host-based mapping gate.
KNOWN_SOURCE_IDS = {
    "dataverse_nl", "dans", "datahuis_wadden", "gbif", "zenodo", "rivm_geonetwork",
}


# =====================================================
# In-memory live job store
# =====================================================
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


# =====================================================
# Phase configuration
# =====================================================
PHASE_DEFINITIONS = [
    {
        "key": "phase1",
        "title": "PHASE 1 — Endpoint Harvesting",
        "status": "WAITING",
        "messages": [],
    },
    {
        "key": "phase2",
        "title": "PHASE 2 — Filtering",
        "status": "WAITING",
        "messages": [],
    },
    {
        "key": "phase3",
        "title": "PHASE 3 — Schema Mapping",
        "status": "WAITING",
        "messages": [],
    },

    {
        "key": "phase4",
        "title": "PHASE 4 — Export",
        "status": "WAITING",
        "messages": [],
    },
]


# =====================================================
# Helpers
# =====================================================
def normalize_host(url: str) -> str:
    m = re.match(r"^https?://([^/]+)", (url or "").strip(), flags=re.I)
    return (m.group(1).lower() if m else "").replace("www.", "")


def is_supported_mapping_url(url: str) -> bool:
    return normalize_host(url) in SUPPORTED_MAPPING_HOSTS


def parse_terms(raw: Optional[str]) -> list[str]:
    """
    Parse comma-separated or line-separated UI input.
    """
    if not raw:
        return []

    parts = re.split(r"[\n,]+", raw)
    out = []
    seen = set()

    for p in parts:
        term = re.sub(r"\s+", " ", p.strip()).lower()
        if term and term not in seen:
            out.append(term)
            seen.add(term)

    return out


def get_keycloak_token() -> str:
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


def _gn_headers(access_token: str) -> dict:
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
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def extract_title_from_jsonld(obj: dict) -> Optional[str]:
    candidates = [
        "Title",
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

        if isinstance(v, str) and v.strip():
            return _norm_text(v)

        if isinstance(v, dict) and isinstance(v.get("@value"), str):
            return _norm_text(v["@value"])

        if isinstance(v, list) and v:
            for it in v:
                if isinstance(it, str) and it.strip():
                    return _norm_text(it)
                if isinstance(it, dict) and isinstance(it.get("@value"), str):
                    return _norm_text(it["@value"])

    return None


def geonetwork_title_exists_debug(title_raw: str, access_token: str) -> bool:
    search_url = f"{GEONETWORK_BASE_URL}/{GEONETWORK_PORTAL}/api/search/records/_search"
    headers = _gn_headers(access_token)

    q = title_raw.replace('"', '\\"')
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

    return len(hits) > 0


def create_filtered_zip_skip_existing(access_token: str) -> Tuple[str, int, int]:
    os.makedirs(FILTERED_DIR, exist_ok=True)

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

        try:
            with open(fp, "r", encoding="utf-8") as f:
                obj = json.load(f)
        except Exception as e:
            print(f"❌ SKIPPED (invalid JSON): {filename} error={e}")
            skipped += 1
            continue

        title = extract_title_from_jsonld(obj)

        print("--------------------------------------------------")
        print(f"📄 FILE: {filename}")
        print(f"🏷 EXTRACTED TITLE: {repr(title)}")

        if not title:
            print("⚠️ NO TITLE FOUND → KEEPING")
            out = os.path.join(FILTERED_DIR, filename)
            with open(out, "w", encoding="utf-8") as fo:
                json.dump(obj, fo, ensure_ascii=False, indent=2)
            kept += 1
            continue

        exists = geonetwork_title_exists_debug(title, access_token)
        if exists:
            print("❌ DECISION: SKIP")
            skipped += 1
            continue

        print("✅ DECISION: KEEP")
        out = os.path.join(FILTERED_DIR, filename)
        with open(out, "w", encoding="utf-8") as fo:
            json.dump(obj, fo, ensure_ascii=False, indent=2)
        kept += 1

    create_zip(FILTERED_DIR, FILTERED_ZIP_PATH)
    print(f"\nFILTER RESULT: kept={kept}, skipped_existing_in_catalogue={skipped}\n")

    return FILTERED_ZIP_PATH, kept, skipped


def _new_phases() -> List[Dict[str, Any]]:
    return deepcopy(PHASE_DEFINITIONS)


def _normalize_phase_status(status: str) -> str:
    status = (status or "").upper().strip()
    if status in {"WAITING", "RUNNING", "DONE", "ERROR"}:
        return status
    return "WAITING"


def _phase_index(phases: List[Dict[str, Any]], phase_key: str) -> int:
    for i, phase in enumerate(phases):
        if phase.get("key") == phase_key:
            return i
    raise KeyError(f"Unknown phase key: {phase_key}")


def _create_job(initial_payload: dict) -> str:
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "queued",
            "logs": [],
            "phases": _new_phases(),
            "result": None,
            "error": None,
            "payload": initial_payload,
            "mapped_records": None,
            "enrich_progress": None,   # NEW
        }
    return job_id


def _append_job_log(job_id: str, message: str) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id]["logs"].append(message)


def _append_phase_message(job_id: str, phase_key: str, message: str) -> None:
    with JOBS_LOCK:
        if job_id not in JOBS:
            return
        try:
            idx = _phase_index(JOBS[job_id]["phases"], phase_key)
        except KeyError:
            return
        JOBS[job_id]["phases"][idx]["messages"].append(message)


def _set_phase_status(job_id: str, phase_key: str, status: str) -> None:
    status = _normalize_phase_status(status)
    with JOBS_LOCK:
        if job_id not in JOBS:
            return
        try:
            idx = _phase_index(JOBS[job_id]["phases"], phase_key)
        except KeyError:
            return
        JOBS[job_id]["phases"][idx]["status"] = status


def _advance_to_phase(job_id: str, new_phase_key: str) -> None:
    with JOBS_LOCK:
        if job_id not in JOBS:
            return

        phases = JOBS[job_id]["phases"]

        try:
            target_idx = _phase_index(phases, new_phase_key)
        except KeyError:
            return

        for i, phase in enumerate(phases):
            if i < target_idx:
                if phase["status"] in {"WAITING", "RUNNING"}:
                    phase["status"] = "DONE"
            elif i == target_idx:
                if phase["status"] != "ERROR":
                    phase["status"] = "RUNNING"
            else:
                if phase["status"] not in {"ERROR", "DONE"}:
                    phase["status"] = "WAITING"


def _mark_phase_running(job_id: str, phase_key: str) -> None:
    _advance_to_phase(job_id, phase_key)


def _complete_phase(job_id: str, phase_key: str) -> None:
    _set_phase_status(job_id, phase_key, "DONE")


def _error_phase(job_id: str, phase_key: str, message: Optional[str] = None) -> None:
    _set_phase_status(job_id, phase_key, "ERROR")
    if message:
        _append_phase_message(job_id, phase_key, message)


def _update_job(job_id: str, **fields) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(fields)


def _get_job(job_id: str) -> Optional[dict]:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return None
        return {
            "status": job["status"],
            "logs": list(job["logs"]),
            "phases": deepcopy(job["phases"]),
            "result": job["result"],
            "error": job["error"],
            "payload": job["payload"],
        }


def _infer_phase_from_message(message: str) -> str:
    """
    Best-effort mapping from plain progress messages to UI phases.
    This lets the phase UI work even if run_harvest still emits only free text.
    """
    msg = (message or "").strip().lower()

    if any(x in msg for x in [
        "llm enrichment",
        "enrichment was used",
        "missing fields",
        "semantic extraction",
    ]):
        return "phase4"

    if any(x in msg for x in [
        "mapping filtered record",
        "schema mapping",
        "mapped to lterlife",
        "lterlife schema",
        "converting extracted nodes to metadata",
    ]):
        return "phase3"

    if any(x in msg for x in [
        "filter",
        "records kept after filtering",
        "applying include",
        "applying exclude",
        "advanced query",
    ]):
        return "phase2"

    if any(x in msg for x in [
        "export",
        "json-ld",
        "jsonld",
        "zip",
        "writing output",
        "creating zip",
    ]):
        return "phase4"

    return "phase1"


def _route_progress_message(job_id: str, message: str) -> None:
    """
    Append raw log and also route the message to the most relevant phase.
    """
    _append_job_log(job_id, message)

    phase_key = _infer_phase_from_message(message)
    _mark_phase_running(job_id, phase_key)
    _append_phase_message(job_id, phase_key, message)


def _finalize_phases_success(job_id: str) -> None:
    with JOBS_LOCK:
        if job_id not in JOBS:
            return

        for phase in JOBS[job_id]["phases"]:
            if phase["status"] == "RUNNING":
                phase["status"] = "DONE"


def _ensure_export_phase_summary(job_id: str, export_format: str) -> None:
    _mark_phase_running(job_id, "phase4")
    _complete_phase(job_id, "phase4")


def _run_harvest_job(
    job_id: str,
    *,
    node_name: str,
    url: str,
    from_date: Optional[str],
    until_date: Optional[str],
    export_format: str,
    filter_mode: str,
    include_terms: str,
    exclude_terms: str,
    filter_query: str,
    source: Optional[str] = None,
    max_records: Optional[int] = None,
) -> None:
    current_phase = "phase1"

    try:
        _update_job(job_id, status="running")

        _mark_phase_running(job_id, "phase1")

        if (source not in KNOWN_SOURCE_IDS) and (not is_supported_mapping_url(url)):
            msg = "So far the schema mapping is not performed for this URL."
            _append_job_log(job_id, msg)
            _complete_phase(job_id, "phase1")
            _mark_phase_running(job_id, "phase3")
            _error_phase(job_id, "phase3", msg)

            _update_job(
                job_id,
                status="error",
                error=msg,
                result={
                    "record_count": 0,
                    "records": [],
                    "filter_info": {
                        "mode": filter_mode,
                        "server_side_applied": False,
                        "client_side_applied": False,
                        "message": msg,
                    },
                },
            )
            return

        filter_payload = {
            "mode": filter_mode,
            "include_terms": parse_terms(include_terms),
            "exclude_terms": parse_terms(exclude_terms),
            "query": (filter_query or "").strip(),
        }

        if filter_mode == "basic":
            _mark_phase_running(job_id, "phase2")
        else:
            _mark_phase_running(job_id, "phase2")

        def progress_callback(phase_key: str, message: str, status: Optional[str] = None) -> None:
            nonlocal current_phase
            current_phase = phase_key

            if status == "RUNNING":
                _advance_to_phase(job_id, phase_key)
            elif status == "DONE":
                _set_phase_status(job_id, phase_key, "DONE")
            elif status == "ERROR":
                _set_phase_status(job_id, phase_key, "ERROR")
            elif status == "WAITING":
                _set_phase_status(job_id, phase_key, "WAITING")
            else:
                # message inside a phase without explicit status change
                with JOBS_LOCK:
                    if job_id in JOBS:
                        phases = JOBS[job_id]["phases"]
                        try:
                            idx = _phase_index(phases, phase_key)
                            if phases[idx]["status"] == "WAITING":
                                _advance_to_phase(job_id, phase_key)
                        except KeyError:
                            pass

            _append_job_log(job_id, message)

        result = run_harvest(
            portal_url=url,
            start_date=from_date or None,
            end_date=until_date or None,
            filter_spec=filter_payload,
            progress_callback=progress_callback,
            run_llm=False,  # LLM enrichment is now a separate, opt-in step after results are shown
            source=source,
            max_records=max_records,
        )

        ui_records = result.get("ui_records", [])
        filter_info = result.get("filter_info", {})
        progress_messages = result.get("progress_messages", [])

        existing_job = _get_job(job_id)
        existing_logs = existing_job["logs"] if existing_job else []
        for msg in progress_messages:
            if msg not in existing_logs:
                _append_job_log(job_id, msg)

        record_count = (
            sum(1 for r in ui_records if isinstance(r, dict) and r.get("separator")) + 1
            if ui_records else 0
        )

        if record_count >= 0:
            _complete_phase(job_id, "phase1")

        if filter_info:
            _mark_phase_running(job_id, "phase2")
            _complete_phase(job_id, "phase2")

        if any("phase3" == _infer_phase_from_message(m) for m in progress_messages) or any(
            _infer_phase_from_message(log) == "phase3" for log in _get_job(job_id)["logs"]
        ):
            _complete_phase(job_id, "phase3")


        _ensure_export_phase_summary(job_id, export_format)
        _finalize_phases_success(job_id)

        _update_job(job_id, mapped_records=result.get("mapped_records"))
        _update_job(
            job_id,
            status="done",
            result={
                "node_name": node_name,
                "url": url,
                "from_date": from_date,
                "until_date": until_date,
                "export_format": export_format,
                "record_count": record_count,
                "records": ui_records,
                "filter_info": filter_info,
            },
        )

    except Exception as e:
        err_msg = f"❌ Pipeline failed: {type(e).__name__}: {e}"
        _append_job_log(job_id, err_msg)
        _error_phase(job_id, current_phase, err_msg)

        _update_job(
            job_id,
            status="error",
            error=f"{type(e).__name__}: {e}",
        )

def _run_enrich_job(job_id: str, mapped_records: list, api_key: Optional[str]) -> None:
    def progress_callback(phase_key: str, message: str, status: Optional[str] = None) -> None:
        _append_job_log(job_id, message)
        if status:
            _set_phase_status(job_id, phase_key, status)

    def on_progress(current: int, total: int) -> None:
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["enrich_progress"] = {"current": current, "total": total, "status": "running"}

    try:
        enrich_result = enrich_and_reexport(
            mapped_records,
            api_key=api_key,
            progress_callback=progress_callback,
            on_progress=on_progress,
        )


        updated_ui_records = enrich_result["ui_records"]
        record_count = (
            sum(1 for r in updated_ui_records if isinstance(r, dict) and r.get("separator")) + 1
            if updated_ui_records else 0
        )

        with JOBS_LOCK:
            JOBS[job_id]["mapped_records"] = enrich_result["mapped_records"]
            if JOBS[job_id]["result"]:
                JOBS[job_id]["result"]["records"] = updated_ui_records
                JOBS[job_id]["result"]["record_count"] = record_count
            JOBS[job_id]["enrich_progress"] = {
                "current": len(mapped_records),
                "total": len(mapped_records),
                "status": "done",
            }

    except Exception as e:
        err_msg = f"❌ LLM enrichment failed: {type(e).__name__}: {e}"
        _append_job_log(job_id, err_msg)
        _error_phase(job_id, "phase4", err_msg)
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["enrich_progress"] = {
                    "current": (JOBS[job_id].get("enrich_progress") or {}).get("current", 0),
                    "total": len(mapped_records),
                    "status": "error",
                    "error": str(e),
                }
# =====================================================
# UI: Home page
# =====================================================
@app.get("/", response_class=HTMLResponse)
def ui_form(request: Request):
    return templates.TemplateResponse(
        "form.html",
        {
            "request": request,
            "filter_mode": "basic",
            "include_terms": "",
            "exclude_terms": "",
            "filter_query": "",
            "progress_messages": [],
            "mapping_warning": None,
        },
    )


# =====================================================
# Harvest: start background job
# =====================================================
@app.post("/run")
def run_harvest_ui(
    node_name: str = Form(...),
    url: str = Form(...),
    from_date: str = Form(None),
    until_date: str = Form(None),
    export_format: str = Form("jsonld"),
    filter_mode: str = Form("basic"),
    include_terms: str = Form(""),
    exclude_terms: str = Form(""),
    filter_query: str = Form(""),
    source: str = Form(""),
    max_records: str = Form(""),
):
    parsed_max_records: Optional[int] = None
    if (max_records or "").strip():
        try:
            parsed_max_records = int(str(max_records).strip())
            if parsed_max_records <= 0:
                parsed_max_records = None
        except ValueError:
            parsed_max_records = None

    """
    Start a background harvest job and return a job_id for polling.
    """
    initial_payload = {
        "node_name": node_name,
        "url": url,
        "from_date": from_date,
        "until_date": until_date,
        "export_format": export_format,
        "filter_mode": filter_mode,
        "include_terms": include_terms,
        "exclude_terms": exclude_terms,
        "filter_query": filter_query,
        "source": source,
        "max_records": parsed_max_records,
    }

    job_id = _create_job(initial_payload)

    thread = threading.Thread(
        target=_run_harvest_job,
        kwargs={
            "job_id": job_id,
            "node_name": node_name,
            "url": url,
            "from_date": from_date,
            "until_date": until_date,
            "export_format": export_format,
            "filter_mode": filter_mode,
            "include_terms": include_terms,
            "exclude_terms": exclude_terms,
            "filter_query": filter_query,
            "source": (source or None),
            "max_records": parsed_max_records,
        },
        daemon=True,
    )
    thread.start()

    return JSONResponse({"job_id": job_id})


# =====================================================
# Harvest: poll job status
# =====================================================
@app.get("/run-status/{job_id}")
def run_status(job_id: str):
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    return JSONResponse(
        {
            "job_id": job_id,
            "status": job["status"],
            "logs": job["logs"],
            "phases": job["phases"],
            "result": job["result"],
            "error": job["error"],
        }
    )


# =====================================================
# LLM Enrichment: on-demand, after results are shown
# =====================================================
@app.post("/enrich-llm/{job_id}")
def enrich_llm(job_id: str, api_key: str = Form(None)):
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    with JOBS_LOCK:
        mapped_records = JOBS.get(job_id, {}).get("mapped_records")
        current_progress = JOBS.get(job_id, {}).get("enrich_progress")

    if not mapped_records:
        raise HTTPException(status_code=400, detail="No mapped records available for this job.")

    if current_progress and current_progress.get("status") == "running":
        return JSONResponse({"started": False, "message": "Enrichment already running."})

    with JOBS_LOCK:
        JOBS[job_id]["enrich_progress"] = {"current": 0, "total": len(mapped_records), "status": "running"}



    thread = threading.Thread(
        target=_run_enrich_job,
        kwargs={"job_id": job_id, "mapped_records": mapped_records, "api_key": (api_key or None)},
        daemon=True,
    )
    thread.start()

    return JSONResponse({"started": True})


@app.get("/enrich-status/{job_id}")
def enrich_status(job_id: str):
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    with JOBS_LOCK:
        progress = deepcopy(JOBS.get(job_id, {}).get("enrich_progress"))

    return JSONResponse({
        "job_id": job_id,
        "progress": progress,
        "result": job["result"],
        "phases": job["phases"],
    })


# =====================================================
# Download: original ZIP
# =====================================================
@app.get("/download/jsonld")
def download_jsonld_zip():
    if not os.path.exists(ZIP_PATH):
        raise HTTPException(status_code=404, detail="ZIP file not found. Run harvest first.")
    return FileResponse(
        ZIP_PATH,
        media_type="application/zip",
        filename="lterlife_jsonld_records.zip",
    )


# =====================================================
# Download: filtered ZIP (after catalogue dedupe)
# =====================================================
@app.get("/download/jsonld-filtered")
def download_jsonld_filtered_zip():
    if not os.path.exists(FILTERED_ZIP_PATH):
        raise HTTPException(status_code=404, detail="Filtered ZIP not found. Push once to generate it.")
    return FileResponse(
        FILTERED_ZIP_PATH,
        media_type="application/zip",
        filename="lterlife_jsonld_records_filtered.zip",
    )


# =====================================================
# Push to GeoNetwork
# =====================================================
@app.post("/push-to-catalog")
def push_to_catalog():
    if not os.path.exists(ZIP_PATH):
        raise HTTPException(status_code=400, detail="ZIP not found. Run harvest first.")

    access_token = get_keycloak_token()
    filtered_zip, kept, skipped = create_filtered_zip_skip_existing(access_token)

    if kept == 0:
        return JSONResponse(
            content={
                "push_report": {
                    "message": "Nothing to push: all harvested records already exist in the catalogue (by title).",
                    "kept": kept,
                    "skipped": skipped,
                    "mode": "skip-if-title-exists",
                }
            }
        )

    BATCH_SIZE = int(os.getenv("GN_IMPORT_BATCH_SIZE", "15"))
    import_url = f"{GEONETWORK_BASE_URL}/{GEONETWORK_PORTAL}/api/records"
    headers = _gn_headers(access_token)

    kept_files = sorted(glob.glob(os.path.join(FILTERED_DIR, "*.json*")))

    if not kept_files:
        report = {
            "message": "Nothing to push: filtered set is empty.",
            "dedupe": {"kept": kept, "skipped": skipped, "mode": "skip-if-title-exists"},
        }
        return JSONResponse(content={"push_report": report})

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

            with zipfile.ZipFile(batch_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for fp in chunk:
                    zf.write(fp, arcname=os.path.basename(fp))

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
                    batch_reports.append({
                        "batch": bidx,
                        "status": resp.status_code,
                        "report": resp.json()
                    })
                except Exception:
                    batch_reports.append({
                        "batch": bidx,
                        "status": resp.status_code,
                        "report": resp.text[:2000]
                    })
            else:
                failed_batches += 1
                batch_reports.append({
                    "batch": bidx,
                    "status": resp.status_code,
                    "error": resp.text[:2000]
                })

    report = {
        "message": "Batch import finished.",
        "batches_total": total_batches,
        "batches_ok": ok_batches,
        "batches_failed": failed_batches,
        "dedupe": {"kept": kept, "skipped": skipped, "mode": "skip-if-title-exists"},
        "batch_reports_preview": batch_reports[:10],
    }

    if ok_batches == 0 and failed_batches > 0:
        first = batch_reports[0]
        return JSONResponse(
            status_code=500,
            content={
                "error": "GeoNetwork batch ZIP import failed.",
                "first_batch_error": first,
                "push_report": report,
            },
        )

    return JSONResponse(content={"push_report": report})