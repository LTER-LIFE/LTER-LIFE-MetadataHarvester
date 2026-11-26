"""
Converters: one real record per source (tests/fixtures, recorded 2026-11-26,
e-mail addresses replaced) mapped to the LTER-LIFE schema.

Contract shared by every mapper:
  - every LTER-LIFE field is present, as {"value": ..., "raw_fields": [...]}
  - exact coverage -> value as-is, partial coverage -> value prefixed with '*'
  - nothing found -> "-"

A known but unfixed mapping bug is recorded as a strict xfail test; once the
bug is fixed the test passes, pytest reports it, and the marker is removed.
"""
import pytest

from converters.dans_mapper import DANS_MAPPING, map_dans_record_to_lterlife
from converters.gbif_mapper import GBIF_MAPPING, map_gbif_record_to_lterlife
from converters.iso19139_mapper import ISO19139_MAPPING, map_iso19139_record_to_lterlife
from converters.zenodo_mapper import ZENODO_MAPPING, map_zenodo_record_to_lterlife

from conftest import load_json, load_text


def as_list(value):
    return value if isinstance(value, list) else [value]


@pytest.fixture(scope="module")
def zenodo():
    return map_zenodo_record_to_lterlife(load_json("zenodo_record.json"))


@pytest.fixture(scope="module")
def gbif():
    return map_gbif_record_to_lterlife(load_text("gbif_eml.xml"))


@pytest.fixture(scope="module")
def iso():
    return map_iso19139_record_to_lterlife(load_text("csw_iso19139_record.xml"))


@pytest.fixture(scope="module")
def dans():
    return map_dans_record_to_lterlife(load_text("dans_oai_dc_record.xml"))


# ---------------------------------------------------------------- contract

ALL_MAPPERS = [
    ("zenodo", ZENODO_MAPPING),
    ("gbif", GBIF_MAPPING),
    ("iso", ISO19139_MAPPING),
    ("dans", DANS_MAPPING),
]


@pytest.mark.parametrize("name, mapping", ALL_MAPPERS)
def test_every_schema_field_is_present(name, mapping, request):
    mapped = request.getfixturevalue(name)
    assert set(mapped) == set(mapping)
    for field, entry in mapped.items():
        assert set(entry) == {"value", "raw_fields"}, field
        assert entry["value"] not in ("", None, []), field


@pytest.mark.parametrize("name, mapping", ALL_MAPPERS)
def test_partial_coverage_values_are_starred(name, mapping, request):
    mapped = request.getfixturevalue(name)
    for field, cfg in mapping.items():
        if cfg["coverage"] == "partial" and mapped[field]["value"] != "-":
            assert all(v.startswith("*") for v in as_list(mapped[field]["value"])), field


@pytest.mark.parametrize("mapper", [map_gbif_record_to_lterlife, map_iso19139_record_to_lterlife,
                                    map_dans_record_to_lterlife])
def test_broken_xml_gives_empty_record_instead_of_crashing(mapper):
    mapped = mapper("<not-closed>")
    assert mapped and all(entry["value"] == "-" for entry in mapped.values())


# ---------------------------------------------------------------- Zenodo

def test_zenodo_core_fields(zenodo):
    assert zenodo["Title"]["value"] == (
        "Data from: Consensus forecasting of intertidal seagrass habitat in the Wadden Sea"
    )
    assert "Zostera marina" in zenodo["Keyword"]["value"]
    assert "Folmer, Eelke O." in zenodo["Creator"]["value"]
    assert "*https://zenodo.org/records/5001184" in as_list(zenodo["Landing page"]["value"])
    assert "*10.5061/dryad.q9c54" in as_list(zenodo["Identifier"]["value"])


def test_zenodo_reads_fields_nested_under_metadata():
    record = {"id": 1, "metadata": {"title": "T", "keywords": ["k1", "k2"]}}
    mapped = map_zenodo_record_to_lterlife(record)
    assert mapped["Title"]["value"] == "T"
    assert mapped["Keyword"]["value"] == ["k1", "k2"]


def test_zenodo_license_is_mapped(zenodo):
    # The REST API nests it: "license": {"id": "cc-zero"}
    assert zenodo["License"]["value"] == "cc-zero"


def test_zenodo_plain_string_license_still_works():
    assert map_zenodo_record_to_lterlife({"license": "CC-BY-4.0"})["License"]["value"] == "CC-BY-4.0"


def test_zenodo_values_are_not_python_dict_reprs(zenodo):
    for field, entry in zenodo.items():
        for value in as_list(entry["value"]):
            assert "{'" not in value, (field, value)


def test_zenodo_list_items_use_their_representative_value(zenodo):
    assert "*Wadden Sea" in zenodo["Spatial coverage"]["value"]              # locations[].place
    assert "*10.1111/1365-2664.12681" in zenodo["Identifier"]["value"]       # related_identifiers[].identifier


def test_zenodo_list_item_without_representative_key_is_flattened():
    from converters.zenodo_mapper import _flatten

    assert _flatten({"dates": [{"start": "2010", "end": "2020"}]}) == {
        "dates.start": ["2010"], "dates.end": ["2020"],
    }


# ---------------------------------------------------------------- GBIF (EML)

def test_gbif_core_fields(gbif):
    assert gbif["Title"]["value"] == "Common Wadden Sea Secretariat: Fishes Checklist"
    assert "*CC0-1.0" in gbif["License"]["value"]


def test_gbif_landing_page_is_built_from_package_id(gbif):
    urls = as_list(gbif["Access URL"]["value"])
    assert "*https://www.gbif.org/dataset/0b2c1ae8-8eb9-4be6-9ea7-33cf15e6259b" in urls


def test_gbif_bare_dois_become_resolvable_urls(gbif):
    assert "*https://doi.org/10.15468/92gf3v" in as_list(gbif["Identifier"]["value"])


def test_gbif_licence_pages_are_never_used_as_landing_page(gbif):
    for field in ("Landing page", "Access URL"):
        for url in as_list(gbif[field]["value"]):
            assert "creativecommons.org" not in url and "spdx.org" not in url


def test_gbif_creator_is_the_full_name_of_each_creator(gbif):
    # Only <creator>; contacts and metadata providers are other roles.
    assert gbif["Creator"]["value"] == "Julia A. Busch"


def test_gbif_creator_without_person_falls_back_to_organisation():
    eml = """<eml:eml xmlns:eml="https://eml.ecoinformatics.org/eml-2.2.0"><dataset>
        <creator><individualName><givenName>A.</givenName><surName>One</surName></individualName></creator>
        <creator><organizationName>Wadden Lab</organizationName></creator>
        <contact><individualName><surName>NotACreator</surName></individualName></contact>
    </dataset></eml:eml>"""
    assert map_gbif_record_to_lterlife(eml)["Creator"]["value"] == ["A. One", "Wadden Lab"]


# ---------------------------------------------------------------- ISO 19139 (CSW)

def test_iso_keywords_and_bounding_box(iso):
    assert "Biodiversiteit" in iso["Keyword"]["value"]
    west, east, south, north = (float(v) for v in iso["Spatial coverage"]["value"])
    assert 3 < west < east < 8 and 50 < south < north < 54   # around the Wadden Sea


def test_iso_title_is_only_the_dataset_title(iso):
    # Not "GEMET - INSPIRE themes" or the cited regulations, which are also <gmd:title>s.
    assert iso["Title"]["value"] == "V2: Drieteenstrandloper"


def test_rivm_mapper_inherits_the_dataset_title():
    from converters.rivm_mapper import map_rivm_record_to_lterlife

    assert map_rivm_record_to_lterlife(load_text("csw_iso19139_record.xml"))["Title"]["value"] == (
        "V2: Drieteenstrandloper"
    )


# ---------------------------------------------------------------- DANS (oai_dc)

def test_dans_core_fields(dans):
    assert dans["Title"]["value"] == "An experimental approach in revisiting the magnetic orientation of cattle"
    assert dans["Identifier"]["value"] == "https://doi.org/10.17026/DANS-225-KK6Y"
    assert dans["Landing page"]["value"] == "*https://doi.org/10.17026/DANS-225-KK6Y"
    assert dans["Creator"]["value"] == "D. Weijers"
