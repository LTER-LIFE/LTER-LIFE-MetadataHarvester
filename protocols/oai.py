from sickle import Sickle
from datetime import datetime


def _oai_list_size(sickle, params) -> int | None:
    """
    Best-effort record count for an OAI-PMH request, via ListIdentifiers.
    Uses the resumptionToken's completeListSize when the result spans several
    pages; for a single-page result it simply counts the identifiers.
    """
    try:
        it = sickle.ListIdentifiers(**params)
        try:
            next(it)
        except StopIteration:
            return 0
        token = getattr(it, "resumption_token", None)
        size = getattr(token, "complete_list_size", None) if token else None
        if size not in (None, ""):
            return int(size)
        return 1 + sum(1 for _ in it)
    except Exception as e:
        if "noRecordsMatch" in type(e).__name__ or "noRecordsMatch" in str(e):
            return 0
        print(f"⚠️ OAI-PMH record count unavailable: {type(e).__name__}: {e}", flush=True)
        return None


def harvest_oai(url, max_records=20, start_date=None, end_date=None, stats: dict | None = None):
    print(f"📚 Harvesting OAI-PMH from {url}")
    print(f"⏱ From: {start_date} | Until: {end_date}")

    results = []

    try:
        sickle = Sickle(url)

        params = {"metadataPrefix": "oai_dc"}
        if start_date:
            params["from"] = start_date
        if end_date:
            params["until"] = end_date

        if stats is not None:
            stats["server_side_request"] = {
                "verb": "ListRecords",
                **params,
                "note": "OAI-PMH cannot filter on keywords; only the date range "
                        "(OAI datestamp = last change in the repository) is applied by the portal.",
            }
            stats["records_found"] = _oai_list_size(sickle, {"metadataPrefix": "oai_dc"})
            stats["after_server_side_filtering"] = (
                _oai_list_size(sickle, params) if (start_date or end_date) else stats["records_found"]
            )

        records = sickle.ListRecords(**params)

        i = 0
        while True:
            if (max_records is not None) and (i >= max_records):
                break
            try:
                record = next(records)
            except StopIteration:
                break
            except Exception as page_error:
                print(f"⚠️ OAI-PMH pagination failed after {len(results)} records "
                      f"(kept anyway): {page_error}", flush=True)
                break

            results.append(record.raw)
            i += 1

    except Exception as e:
        print(f"⚠️ OAI-PMH harvest failed before any records were collected: {e}", flush=True)

    print(f"✅ Total OAI records harvested: {len(results)}")
    return results
