# protocols/gbif.py
# ------------------------------------------------------
# GBIF harvester
# ------------------------------------------------------
# GBIF's registry does expose OAI-PMH, but it cannot do boolean keyword
# filtering. The REST search API can (server-side `q=`), so we:
#   1) page through https://api.gbif.org/v1/dataset/search?q=<terms>
#   2) optionally drop hits outside the requested date range (client-side,
#      on the search hit's `modified` / `created` timestamp)
#   3) fetch each surviving dataset's EML document
#      (https://api.gbif.org/v1/dataset/{key}/document) as raw XML
#
# The EML XML strings are returned so converters/gbif_mapper.py
# (map_gbif_record_to_lterlife) can parse them exactly like any other
# XML-based source.

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

DOCUMENT_URL = "https://api.gbif.org/v1/dataset/{key}/document"

PAGE_SIZE = 100
MAX_PAGES = 100
REQUEST_DELAY = 0.2
MAX_RETRIES_PER_PAGE = 5
DOC_WORKERS = 8  # parallel EML-document fetches


def _in_date_range(hit: dict, start_date, end_date) -> bool:
    if not start_date and not end_date:
        return True

    stamp = (hit.get("modified") or hit.get("created") or "")[:10]
    if not stamp:
        # No usable timestamp -> keep it, let downstream filtering decide.
        return True

    if start_date and stamp < str(start_date)[:10]:
        return False
    if end_date and stamp > str(end_date)[:10]:
        return False
    return True


def harvest_gbif(search_url: str,
                 max_records: int = None,
                 start_date=None,
                 end_date=None,
                 include_terms: list[str] | None = None,
                 exclude_terms: list[str] | None = None,
                 progress_cb=None) -> list[str]:
    print(f"🐝 Harvesting GBIF from {search_url}")
    print(f"⏱ From: {start_date} | Until: {end_date}")
    print(f"🔎 Include terms: {include_terms}")

    def _emit(msg: str) -> None:
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass

    q = " ".join(t.strip() for t in (include_terms or []) if t and t.strip())

    headers = {"User-Agent": "LTER-LIFE-Harvester/1.0"}
    session = requests.Session()

    keys: list[str] = []
    seen: set[str] = set()
    offset = 0

    for page in range(MAX_PAGES):
        params = {"limit": PAGE_SIZE, "offset": offset}
        if q:
            params["q"] = q

        print(f"  → search page {page + 1} (offset {offset}, collected {len(keys)})", flush=True)
        _emit(f"GBIF: searching page {page + 1} (matched {len(keys)} datasets so far).")

        retries = 0
        while True:
            resp = session.get(search_url, params=params, headers=headers, timeout=30)
            if resp.status_code == 429:
                retries += 1
                if retries > MAX_RETRIES_PER_PAGE:
                    print("⚠️ GBIF rate-limited too many times, stopping search early.", flush=True)
                    resp = None
                    break
                wait = int(resp.headers.get("Retry-After", 5))
                print(f"⏳ GBIF 429, waiting {wait}s (retry {retries}/{MAX_RETRIES_PER_PAGE})", flush=True)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break

        if resp is None:
            break

        data = resp.json()
        results = data.get("results", []) or []
        if page == 0:
            print(f"🔎 GBIF reports {data.get('count', '?')} datasets for q={q or '(none)'}", flush=True)
            _emit(f"GBIF reports {data.get('count', '?')} datasets for the current query.")

        if not results:
            break

        for hit in results:
            key = hit.get("key")
            if not key or key in seen:
                continue
            if not _in_date_range(hit, start_date, end_date):
                continue
            seen.add(key)
            keys.append(key)
            if max_records and len(keys) >= max_records:
                break

        if max_records and len(keys) >= max_records:
            break
        if data.get("endOfRecords"):
            break

        offset += PAGE_SIZE
        time.sleep(REQUEST_DELAY)

    print(f"📦 Fetching EML for {len(keys)} GBIF datasets", flush=True)
    _emit(f"GBIF: fetching metadata for {len(keys)} datasets…")

    def _fetch_one(key: str):
        try:
            r = session.get(DOCUMENT_URL.format(key=key), headers=headers, timeout=30)
            r.raise_for_status()
            return key, r.text
        except Exception as e:
            print(f"⚠️ Skipped GBIF dataset {key}: {type(e).__name__}: {e}", flush=True)
            return key, None

    records: list[str] = []
    done = 0
    with ThreadPoolExecutor(max_workers=DOC_WORKERS) as pool:
        futures = [pool.submit(_fetch_one, k) for k in keys]
        for fut in as_completed(futures):
            done += 1
            _key, text = fut.result()
            if text:
                records.append(text)
            if done % 25 == 0 or done == len(keys):
                print(f"  … {done}/{len(keys)} EML documents fetched", flush=True)
                _emit(f"GBIF: {done}/{len(keys)} dataset documents fetched.")

    print(f"✅ Total GBIF records harvested: {len(records)}")
    return records
