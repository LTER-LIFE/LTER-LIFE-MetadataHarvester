import requests
import xml.etree.ElementTree as ET


def harvest_geonetwork(url, max_records=None):
    headers = {"Accept": "application/json, application/xml, text/xml, application/rdf+xml"}
    r = requests.get(url, timeout=15, headers=headers, verify=False)
    ctype = r.headers.get("Content-Type", "").lower()

    if "json" in ctype:
        data = r.json()
        records = data.get("records", data)
        if max_records is not None:
            return records[:max_records]
        return records

    elif "xml" in ctype or "rdf" in ctype:
        root = ET.fromstring(r.text)
        datasets = []
        for el in root.findall(".//*"):
            if (max_records is not None) and (len(datasets) >= max_records):
                break
            datasets.append(el.tag)
        return datasets

    return []