from sickle import Sickle
from datetime import datetime

def harvest_oai(url, max_records=20, start_date=None, end_date=None):
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