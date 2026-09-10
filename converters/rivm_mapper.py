# converters/rivm_mapper.py
# ------------------------------------------------------
# RIVM (data.rivm.nl -- GeoNetwork CSW, ISO 19139)  ->  LTER-LIFE
# ------------------------------------------------------
# data.rivm.nl serves ISO 19139 (gmd:MD_Metadata) records over CSW
# (verified against a live GetRecords response), so this mapper reuses the
# shared ISO 19139 collector/mapper and only overrides coverage + a few
# source tags to match lterlife_template__mapping_Parinaz_RIVM (Arnout).xlsx:
#
#   Resource type / Publisher / Spatial resolution / Spatial coverage /
#   Dataset format / Size            -> No coverage
#   Reference system ("Geografische dekking")  -> Exactly
#   Responsible party ("Auteur: Affiliatie")   -> Exactly (organisation, not person)
#
# (A previous version of this file assumed a DCAT-AP shape; that turned out
#  not to match what the RIVM CSW actually returns.)

from copy import deepcopy
from typing import Dict, List, Union

from converters.iso19139_mapper import ISO19139_MAPPING, _map_with


RIVM_ISO_MAPPING: Dict[str, Dict[str, Union[List[str], str]]] = deepcopy(ISO19139_MAPPING)

RIVM_ISO_MAPPING["Language (metadata)"]["coverage"] = "partial"
RIVM_ISO_MAPPING["Email of the responsible organization or individual"]["coverage"] = "exact"
RIVM_ISO_MAPPING["Identifier"]["coverage"] = "exact"
RIVM_ISO_MAPPING["Resource type"] = {"sources": [], "coverage": "none"}
RIVM_ISO_MAPPING["Publisher"] = {"sources": [], "coverage": "none"}
RIVM_ISO_MAPPING["spatialResolutionInMeters"] = {"sources": [], "coverage": "none"}
RIVM_ISO_MAPPING["Spatial coverage"] = {"sources": [], "coverage": "none"}
RIVM_ISO_MAPPING["Reference system"] = {
    "sources": ["geographicdescription", "geographicidentifier", "code"],
    "coverage": "exact",
}
RIVM_ISO_MAPPING["Access URL"]["coverage"] = "partial"
RIVM_ISO_MAPPING["Dataset format (distributionFormat)"] = {"sources": [], "coverage": "none"}


def map_rivm_record_to_lterlife(xml_string: str) -> Dict[str, Union[str, List[str]]]:
    """Maps one RIVM ISO 19139 XML record into the LTER-LIFE schema."""
    return _map_with(RIVM_ISO_MAPPING, xml_string)
