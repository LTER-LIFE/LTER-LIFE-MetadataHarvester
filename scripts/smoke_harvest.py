"""
Smoke test: run the harvest -> filter -> map pipeline against every source
in the UI dropdown, with a tiny record cap, and print coverage.

Usage:
    .venv/bin/python scripts/smoke_harvest.py
    .venv/bin/python scripts/smoke_harvest.py zenodo gbif
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harvesterv3_Frontend import run_harvest, SOURCES  # noqa: E402

# Empty by default: OAI/CSW cap record count BEFORE the client-side keyword
# filter, so a selective term + tiny cap would leave nothing to map. Pass a
# term as the 2nd arg pair only when testing filtering specifically.
INCLUDE_TERMS = []
MAX_RECORDS = 3


def summarize(source_id: str) -> None:
    cfg = SOURCES[source_id]
    print("\n" + "=" * 78)
    print(f"SOURCE: {source_id}  ({cfg['label']})  -> {cfg['api']}")
    print("=" * 78)

    result = run_harvest(
        portal_url=cfg["display_url"],
        start_date=None,
        end_date=None,
        max_records=MAX_RECORDS,
        filter_spec={"mode": "basic", "include_terms": INCLUDE_TERMS, "exclude_terms": []},
        source=source_id,
    )

    mapped = result.get("mapped_records") or []
    fi = result.get("filter_info", {})
    print(f"harvested_before_filtering = {fi.get('harvested_before_filtering')}")
    print(f"kept_after_filtering       = {fi.get('kept_after_filtering')}")
    print(f"mapped records             = {len(mapped)}")

    if not mapped:
        print("!! no records mapped")
        return

    rec = mapped[0]
    present = [f for f, p in rec.items()
              if isinstance(p, dict) and str(p.get("value")).strip() not in ("-", "", "None")]
    missing = [f for f in rec if f not in present]
    print(f"record #1: {len(present)}/{len(rec)} fields populated")
    print("  populated:", ", ".join(present))
    print("  missing  :", ", ".join(missing))
    for key in ("Title", "Description", "Identifier", "Keyword"):
        if key in rec:
            v = rec[key].get("value")
            v = v if isinstance(v, str) else (v[0] if v else "")
            print(f"  {key}: {str(v)[:110]}")


def main() -> None:
    wanted = sys.argv[1:] or list(SOURCES.keys())
    failures = []
    for sid in wanted:
        if sid not in SOURCES:
            print(f"unknown source id: {sid}")
            continue
        try:
            summarize(sid)
        except Exception as e:
            failures.append((sid, f"{type(e).__name__}: {e}"))
            print(f"!! {sid} FAILED: {type(e).__name__}: {e}")

    print("\n" + "#" * 78)
    if failures:
        print("FAILURES:")
        for sid, err in failures:
            print(f"  {sid}: {err}")
        sys.exit(1)
    print("all sources completed without exceptions")


if __name__ == "__main__":
    main()
