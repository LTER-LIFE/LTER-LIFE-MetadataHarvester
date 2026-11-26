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


def _gbif_queries(clauses: list[list[str]]) -> list[str]:
    """
    q= searches that cover the keyword clauses ((a OR b) AND c ...).

    GBIF's q is AND-like across words and has no OR operator (measured:
    "wadden bird" ~ "wadden AND bird", while "wadden OR bird" treats OR as a
    word). So:
      - single-term clauses are combined into ONE AND-like search;
      - if every clause is an OR-group, run one search per term of the
        smallest group and union the results.
    Whatever the pre-filter leaves out is still enforced client-side.
    """
    if not clauses:
        return [""]
    units = [c[0] for c in clauses if len(c) == 1]
    if units:
        return [" ".join(dict.fromkeys(units))]
    return list(min(clauses, key=len))


def harvest_gbif(search_url: str,
                 max_records: int = None,
                 start_date=None,
                 end_date=None,
                 include_terms: list[str] | None = None,
                 exclude_terms: list[str] | None = None,
                 progress_cb=None,
                 stats: dict | None = None,
                 clauses: list[list[str]] | None = None) -> list[str]:
    print(f"🐝 Harvesting GBIF from {search_url}")
    print(f"⏱ From: {start_date} | Until: {end_date}")
    print(f"🔎 Include terms: {include_terms}")

    def _emit(msg: str) -> None:
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass

    if clauses is None:
        clauses = [list(include_terms)] if include_terms else []
    clauses = [[t.strip() for t in c if t and t.strip()] for c in clauses]
    clauses = [c for c in clauses if c]
    queries = _gbif_queries(clauses)
    # With several searches (OR-groups) split the cap between them.
    per_query_limit = (-(-max_records // len(queries)) if max_records else None)

    headers = {"User-Agent": "LTER-LIFE-Harvester/1.0"}
    session = requests.Session()

    if stats is not None:
        stats["server_side_request"] = {
            "q": " | ".join(q or "(none)" for q in queries),
            "note": "Positive keyword terms are sent as q= (GBIF's q is AND-like and has no OR, "
                    "so OR-groups run as separate searches). Date range is checked on the "
                    "search hit's 'modified' date before downloading EML (pre-download). "
                    "Exclude terms, NOT-parts and the exact boolean query are applied after download.",
        }
        try:
            r0 = session.get(search_url, params={"limit": 0}, headers=headers, timeout=30)
            r0.raise_for_status()
            stats["records_found"] = int(r0.json().get("count"))
        except Exception as e:
            print(f"⚠️ GBIF total count unavailable: {type(e).__name__}: {e}", flush=True)
            stats["records_found"] = None

    keys: list[str] = []
    seen: set[str] = set()
    counts_per_query: list = []
    hits_examined = 0

    def _search(q: str, limit: int | None) -> None:
        """Page through one q= search, adding new dataset keys until `limit` new ones
        (or the overall max_records) have been collected."""
        nonlocal hits_examined
        offset = 0
        added = 0
        for page in range(MAX_PAGES):
            params = {"limit": PAGE_SIZE, "offset": offset}
            if q:
                params["q"] = q

            print(f"  → search page {page + 1} q={q or '(none)'!r} (offset {offset}, collected {len(keys)})", flush=True)
            _emit(f"GBIF: searching page {page + 1} (matched {len(keys)} datasets so far).")

            retries = 0
            while True:
                try:
                    resp = session.get(search_url, params=params, headers=headers, timeout=30)
                except requests.exceptions.RequestException as e:
                    # Network trouble (timeout, DNS, reset): back off and retry, like Zenodo.
                    retries += 1
                    if retries > MAX_RETRIES_PER_PAGE:
                        print(f"⚠️ GBIF search failed {MAX_RETRIES_PER_PAGE} times ({type(e).__name__}), "
                              f"stopping this search with {len(keys)} datasets.", flush=True)
                        _emit(f"GBIF: search stopped after repeated connection errors ({len(keys)} datasets kept).")
                        return
                    wait = min(5 * retries, 30)
                    print(f"🌐 GBIF request error ({type(e).__name__}), retrying in {wait}s "
                          f"(attempt {retries}/{MAX_RETRIES_PER_PAGE})", flush=True)
                    time.sleep(wait)
                    continue

                if resp.status_code == 429 or resp.status_code in (500, 502, 503, 504):
                    retries += 1
                    if retries > MAX_RETRIES_PER_PAGE:
                        print(f"⚠️ GBIF returned {resp.status_code} too many times, stopping search early.",
                              flush=True)
                        return
                    wait = int(resp.headers.get("Retry-After", 0)) or min(5 * retries, 30)
                    print(f"⏳ GBIF {resp.status_code}, waiting {wait}s (retry {retries}/{MAX_RETRIES_PER_PAGE})",
                          flush=True)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                break

            data = resp.json()
            results = data.get("results", []) or []
            if page == 0:
                print(f"🔎 GBIF reports {data.get('count', '?')} datasets for q={q or '(none)'}", flush=True)
                _emit(f"GBIF reports {data.get('count', '?')} datasets for q={q or '(none)'}.")
                counts_per_query.append(data.get("count") if isinstance(data.get("count"), int) else None)

            if not results:
                return

            for hit in results:
                key = hit.get("key")
                if not key or key in seen:
                    continue
                hits_examined += 1
                if not _in_date_range(hit, start_date, end_date):
                    continue
                seen.add(key)
                keys.append(key)
                added += 1
                if (max_records and len(keys) >= max_records) or (limit and added >= limit):
                    return

            if data.get("endOfRecords"):
                return

            offset += PAGE_SIZE
            time.sleep(REQUEST_DELAY)

    for q in queries:
        _search(q, per_query_limit)
        if max_records and len(keys) >= max_records:
            break

    if stats is not None:
        # One search -> its count is the server-side match count. Several
        # searches overlap, so no honest single number exists (None, not guessed).
        stats["after_server_side_filtering"] = (
            counts_per_query[0] if len(queries) == 1 and counts_per_query else None
        )

    if stats is not None:
        stats["pre_download"] = {
            "applies": bool(start_date or end_date),
            "filter": "date range on search-hit 'modified'",
            "search_hits_examined": hits_examined,
            "kept": len(keys),
        }

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
