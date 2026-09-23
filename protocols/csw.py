# protocols/csw.py
# ------------------------------------------------------
# CSW 2.0.2 harvester (GeoNetwork catalogues: Datahuis Waddenzee, RIVM)
# ------------------------------------------------------
# Pulls full ISO 19139 (gmd:MD_Metadata) records, page by page, and returns
# them as raw XML strings so converters/iso19139_mapper.py can parse them.
#
# Server-side filtering (best effort, degrades gracefully):
#   - date range  -> apiso:Modified  >= / <=
#   - include terms -> OR of csw:AnyText LIKE %term%
# Exclude terms are left to the generic client-side filter in
# harvesterv3_Frontend.apply_filter_to_raw_records().

from owslib.csw import CatalogueServiceWeb
from owslib.fes import (
    PropertyIsLike,
    PropertyIsGreaterThanOrEqualTo,
    PropertyIsLessThanOrEqualTo,
    And,
    Or,
)

ISO_OUTPUT_SCHEMA = "http://www.isotc211.org/2005/gmd"
PAGE_SIZE = 50
MAX_PAGES = 200


def _build_constraints(start_date, end_date, include_terms):
    ands = []

    if start_date:
        ands.append(PropertyIsGreaterThanOrEqualTo("apiso:Modified", str(start_date)))
    if end_date:
        ands.append(PropertyIsLessThanOrEqualTo("apiso:Modified", str(end_date)))

    terms = [t.strip() for t in (include_terms or []) if t and t.strip()]
    if terms:
        likes = [PropertyIsLike("csw:AnyText", f"%{t}%") for t in terms]
        ands.append(likes[0] if len(likes) == 1 else Or(likes))

    if not ands:
        return []
    if len(ands) == 1:
        return ands
    return [And(ands)]


def _describe_constraints(start_date, end_date, include_terms) -> str:
    """Human-readable form of the OGC filter built by _build_constraints()."""
    parts = []
    terms = [t.strip() for t in (include_terms or []) if t and t.strip()]
    if terms:
        parts.append("(" + " OR ".join(f"csw:AnyText LIKE '%{t}%'" for t in terms) + ")")
    if start_date:
        parts.append(f"apiso:Modified >= '{start_date}'")
    if end_date:
        parts.append(f"apiso:Modified <= '{end_date}'")
    return " AND ".join(parts) if parts else "(no filter)"


def _count_matches(csw, constraints) -> int | None:
    """Best-effort 'hits only' request: how many records match, without fetching them."""
    try:
        kwargs = dict(resulttype="hits", maxrecords=1)
        if constraints:
            kwargs["constraints"] = constraints
        csw.getrecords2(**kwargs)
        return int(csw.results.get("matches", 0))
    except Exception as e:
        print(f"⚠️ CSW hit count unavailable: {type(e).__name__}: {e}", flush=True)
        return None


def _records_as_xml(csw) -> list[str]:
    out = []
    for rec in csw.records.values():
        xml = getattr(rec, "xml", None)
        if xml is None:
            continue
        if isinstance(xml, bytes):
            xml = xml.decode("utf-8", errors="replace")
        out.append(xml)
    return out


def _page(csw, constraints, outputschema, startposition, page_size):
    kwargs = dict(
        esn="full",
        startposition=startposition,
        maxrecords=page_size,
    )
    if constraints:
        kwargs["constraints"] = constraints
    if outputschema:
        kwargs["outputschema"] = outputschema
    csw.getrecords2(**kwargs)


def harvest_csw(csw_url: str,
                max_records: int = None,
                start_date=None,
                end_date=None,
                include_terms: list[str] | None = None,
                exclude_terms: list[str] | None = None,
                stats: dict | None = None) -> list[str]:
    print(f"🗂  Harvesting CSW from {csw_url}")
    print(f"⏱ From: {start_date} | Until: {end_date}")
    print(f"🔎 Include terms: {include_terms}")

    try:
        csw = CatalogueServiceWeb(csw_url, timeout=60)
    except Exception as e:
        print(f"⚠️ Could not open CSW endpoint: {type(e).__name__}: {e}", flush=True)
        return []

    constraints = _build_constraints(start_date, end_date, include_terms)

    if stats is not None:
        stats["server_side_request"] = {
            "filter": _describe_constraints(start_date, end_date, include_terms),
            "note": "Exclude terms and advanced queries are not sent to the portal; "
                    "they are applied after download.",
        }
        stats["records_found"] = _count_matches(csw, [])

    # Try, in order: ISO output + constraints -> ISO output only -> defaults.
    attempts = [
        (constraints, ISO_OUTPUT_SCHEMA),
        ([], ISO_OUTPUT_SCHEMA),
        ([], None),
    ]

    records: list[str] = []
    page_size = min(PAGE_SIZE, max_records) if max_records else PAGE_SIZE

    for attempt_idx, (cons, schema) in enumerate(attempts, start=1):
        records = []
        startposition = 1
        first_page_matches = None
        try:
            for _ in range(MAX_PAGES):
                _page(csw, cons, schema, startposition, page_size)
                matched = int(csw.results.get("matches", 0))
                if startposition == 1:
                    first_page_matches = matched
                if attempt_idx == 1 or startposition == 1:
                    print(f"  → CSW attempt {attempt_idx}: {matched} matched, "
                          f"position {startposition}", flush=True)

                records.extend(_records_as_xml(csw))

                if max_records and len(records) >= max_records:
                    records = records[:max_records]
                    break

                nextrecord = int(csw.results.get("nextrecord", 0) or 0)
                if nextrecord <= 0 or nextrecord <= startposition:
                    break
                startposition = nextrecord

            if records or (int(csw.results.get("matches", 0)) == 0):
                # Successful call (even if it legitimately returned nothing).
                if stats is not None:
                    stats["after_server_side_filtering"] = first_page_matches
                    if attempt_idx > 1 and cons != constraints:
                        stats["server_side_filter_dropped"] = True
                        stats["server_side_request"]["note"] = (
                            "The filtered CSW request failed, so the portal was queried "
                            "WITHOUT any filter (no keywords, no date range)."
                        )
                break
        except Exception as e:
            print(f"⚠️ CSW attempt {attempt_idx} failed "
                  f"({type(e).__name__}: {e}); trying a simpler request.", flush=True)
            records = []

    print(f"✅ Total CSW records harvested: {len(records)}")
    return records
