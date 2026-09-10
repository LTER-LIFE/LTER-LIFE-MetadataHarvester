# converters/datahuiswadden_mapper.py
# ------------------------------------------------------
# Datahuis Waddenzee (GeoNetwork, ISO 19139)  ->  LTER-LIFE
# ------------------------------------------------------
# Datahuis Waddenzee is a standard GeoNetwork catalogue serving ISO 19139
# (gmd:MD_Metadata) over CSW, so the mapping is exactly the shared ISO
# 19139 mapper. This thin wrapper exists so the source keeps its own
# named entry point (matching LTER-LIFE_Datahuis_Wadden_mapping_REVIEWED_corrected.xlsx).

from converters.iso19139_mapper import (
    map_iso19139_record_to_lterlife as map_datahuiswadden_record_to_lterlife,
)

__all__ = ["map_datahuiswadden_record_to_lterlife"]
