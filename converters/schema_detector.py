import xml.etree.ElementTree as ET


def detect_schema(xml_string: str) -> str:
    """
    Detects metadata schema based on XML namespaces and root elements.
    Returns: 'dublin_core', 'iso19115', 'csw', or 'generic'
    """

    try:
        root = ET.fromstring(xml_string)
    except ET.ParseError:
        return "generic"

    xml_text = xml_string.lower()

    # ✅ Dublin Core (OAI-DC)
    if "oai_dc:dc" in xml_text or "http://www.openarchives.org/oai/2.0/oai_dc/" in xml_text:
        return "dublin_core"

    # ✅ ISO 19115 / 19139
    if "gmd:md_metadata" in xml_text or "http://www.isotc211.org/2005/gmd" in xml_text:
        return "iso19115"

    # ✅ CSW Records
    if "csw:record" in xml_text or "http://www.opengis.net/cat/csw" in xml_text:
        return "csw"

    # Add inside detect_schema():
    if "zenodo.org" in xml_text or '"conceptrecid"' in xml_text:
        return "zenodo"

    # ✅ Fallback
    return "generic"
