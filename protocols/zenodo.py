# protocols/zenodo.py
import time
import requests


def _build_query(start_date=None, end_date=None, include_terms=None) -> str:
    """
    Builds the Zenodo search query string, combining keyword terms and an
    optional date range.

    - Keyword terms are OR'd together, then AND'd with the date range.
    - Date range uses publication_date bracket syntax as a first attempt.
      If the printed 'total hits' diagnostic in harvest_zenodo() shows this
      isn't actually narrowing results, that syntax needs revisiting —
      but keyword-only filtering should work reliably regardless.
    """
    clauses = []

    if include_terms:
        cleaned = [t.strip() for t in include_terms if t and t.strip()]
        if cleaned:
            term_clause = " OR ".join(f'"{t}"' for t in cleaned)
            clauses.append(f"({term_clause})")

    if start_date or end_date:
        lo = start_date or "*"
        hi = end_date or "*"
        clauses.append(f"publication_date:[{lo} TO {hi}]")

    return " AND ".join(clauses)


def _count_all_records(api_url: str, headers: dict) -> int | None:
    """Best-effort total number of records in Zenodo (no query at all)."""
    try:
        resp = requests.get(api_url, params={"size": 1}, headers=headers, timeout=90)
        resp.raise_for_status()
        return int(resp.json().get("hits", {}).get("total"))
    except Exception as e:
        print(f"⚠️ Zenodo total count unavailable: {type(e).__name__}: {e}", flush=True)
        return None


def harvest_zenodo(api_url: str, max_records: int = None,
                    start_date=None, end_date=None,
                    include_terms: list[str] | None = None,
                    progress_cb=None,
                    stats: dict | None = None) -> list[dict]:
    print(f"📚 Harvesting Zenodo from {api_url}")
    print(f"⏱ From: {start_date} | Until: {end_date}")
    print(f"🔎 Include terms: {include_terms}")

    def _emit(msg: str) -> None:
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass

    query = _build_query(start_date, end_date, include_terms)
    # Unauthenticated zenodo.org caps page size at 25 (400 otherwise).
    page_size = 25
    if max_records:
        page_size = max(1, min(page_size, int(max_records)))
    params = {"size": page_size, "page": 1}
    if query:
        params["q"] = query

    headers = {"User-Agent": "LTER-LIFE-Harvester/1.0"}

    if stats is not None:
        stats["server_side_request"] = {
            "q": query or "(none)",
            "note": "Exclude terms and advanced queries are not sent to the portal; "
                    "they are applied after download.",
        }
        stats["records_found"] = _count_all_records(api_url, headers)
    results = []
    MAX_PAGES = 200
    REQUEST_DELAY = 1.0
    MAX_RETRIES_PER_PAGE = 5
    REQUEST_TIMEOUT = 90  # zenodo.org search can be slow (15-25s/page seen in practice)

    page_count = 0
    while page_count < MAX_PAGES:
        page_count += 1
        print(f"  → fetching page {params['page']} (collected so far: {len(results)})", flush=True)
        _emit(f"Zenodo: requesting page {params['page']} (collected {len(results)} so far).")

        retries = 0
        while True:
            try:
                resp = requests.get(api_url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            except requests.exceptions.RequestException as e:
                retries += 1
                if retries > MAX_RETRIES_PER_PAGE:
                    print(f"⚠️ Giving up on page {params['page']} after {MAX_RETRIES_PER_PAGE} "
                          f"connection errors ({e}).", flush=True)
                    print(f"✅ Total Zenodo records harvested: {len(results)}")
                    return results
                wait = min(5 * retries, 30)
                print(f"🌐 Zenodo request error ({type(e).__name__}). Retrying page {params['page']} "
                      f"in {wait}s (attempt {retries}/{MAX_RETRIES_PER_PAGE})", flush=True)
                time.sleep(wait)
                continue

            # 429 rate limit or 5xx gateway/timeout errors from zenodo.org: back off and retry.
            if resp.status_code == 429 or resp.status_code in (500, 502, 503, 504):
                retries += 1
                if retries > MAX_RETRIES_PER_PAGE:
                    print(f"⚠️ Giving up on page {params['page']} after {MAX_RETRIES_PER_PAGE} retries "
                          f"(last status {resp.status_code}).", flush=True)
                    print(f"✅ Total Zenodo records harvested: {len(results)}")
                    return results

                retry_after = int(resp.headers.get("Retry-After", 0)) or min(5 * retries, 30)
                print(f"⏳ Zenodo returned {resp.status_code}. Waiting {retry_after}s before retrying "
                      f"page {params['page']} (attempt {retries}/{MAX_RETRIES_PER_PAGE})", flush=True)
                time.sleep(retry_after)
                continue

            resp.raise_for_status()
            break

        data = resp.json()

        if params["page"] == 1:
            total = data.get("hits", {}).get("total", "?")
            if stats is not None and isinstance(total, int):
                stats["after_server_side_filtering"] = total
            print(f"🔎 Query: {params.get('q', '(none)')} → total hits reported by Zenodo: {total}", flush=True)
            _emit(f"Zenodo reports {total} matching records for the current query.")

        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            break

        results.extend(hits)
        _emit(f"Zenodo: {len(results)} records fetched.")

        if max_records and len(results) >= max_records:
            results = results[:max_records]
            break

        if not data.get("links", {}).get("next"):
            break

        params["page"] += 1
        time.sleep(REQUEST_DELAY)
    else:
        print(f"⚠️ Hit MAX_PAGES={MAX_PAGES} safety ceiling, stopping harvest early.", flush=True)

    print(f"✅ Total Zenodo records harvested: {len(results)}")
    return results