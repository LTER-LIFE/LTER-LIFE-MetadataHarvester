from dotenv import load_dotenv
load_dotenv()

import os
import time
import requests
from typing import Dict, Any, Optional

LLM_BASE_URL = os.getenv("LLM_HARVESTER_BASE_URL", "http://127.0.0.1:8010").rstrip("/")
LLM_API_KEY = os.getenv("LLM_HARVESTER_API_KEY", "")
#LLM_MODEL = os.getenv("LLM_HARVESTER_MODEL", "surf-default-text-large")
LLM_MODEL = os.getenv("LLM_HARVESTER_MODEL", "surf-Sehyo/Qwen3.5-122B-A10B-NVFP4")

LLM_TIMEOUT_SEC = int(os.getenv("LLM_HARVESTER_TIMEOUT_SEC", "120"))
LLM_POLL_INTERVAL_SEC = float(os.getenv("LLM_HARVESTER_POLL_INTERVAL_SEC", "2"))

class LLMHarvesterError(RuntimeError):
    pass

def submit_job(url: str, model: Optional[str] = None, api_key: Optional[str] = None) -> str:
    key = api_key or LLM_API_KEY
    if not key:
        raise LLMHarvesterError("No LLM API key available (neither user-supplied nor LLM_HARVESTER_API_KEY default).")

    r = requests.post(
        f"{LLM_BASE_URL}/jobs/",
        headers={"Content-Type": "application/json", "X-API-Key": key},
        json={"model": model or LLM_MODEL, "url": url},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    job_id = data.get("job_id")
    if not job_id:
        raise LLMHarvesterError(f"No job_id returned: {data}")
    return job_id

def poll_result(job_id: str) -> Dict[str, Any]:
    deadline = time.time() + LLM_TIMEOUT_SEC

    while time.time() < deadline:
        s = requests.get(f"{LLM_BASE_URL}/jobs/{job_id}", timeout=15)
        s.raise_for_status()
        status = s.json().get("status")

        if status == "success":
            res = requests.get(f"{LLM_BASE_URL}/jobs/{job_id}/result", timeout=30)
            res.raise_for_status()
            return res.json()

        if status in ("failure", "failed", "error"):
            try:
                err = requests.get(f"{LLM_BASE_URL}/jobs/{job_id}/result", timeout=30).json()
            except Exception:
                err = {"detail": "failed, could not fetch error detail"}
            raise LLMHarvesterError(f"LLM job failed: {err}")

        time.sleep(LLM_POLL_INTERVAL_SEC)

    raise LLMHarvesterError(f"Timed out waiting for LLM job {job_id}")

def enrich_url(url: str, api_key: Optional[str] = None) -> Dict[str, Any]:
    """
    Returns a small dict you can attach to your record.
    If api_key is None, falls back to the server's default SURF/WILLMA key.
    """
    job_id = submit_job(url, api_key=api_key)
    payload = poll_result(job_id)
    return {
        "job_id": payload.get("job_id"),
        "model": payload.get("model"),
        "result": payload.get("result", {}),
        "logs": payload.get("logs", ""),
    }