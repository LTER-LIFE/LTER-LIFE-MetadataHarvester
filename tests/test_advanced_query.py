"""
Tests for the advanced boolean query: evaluator, server-side pre-filter derivation,
and the per-protocol query builders.

Run:  pytest tests/test_advanced_query.py
"""
import random

import pytest

import harvesterv3_Frontend as H
from protocols.csw import _describe_constraints
from protocols.gbif import _gbif_queries
from protocols.zenodo import _build_query

TEXT = H._normalize_filter_text(
    "<title>Wadden Sea bird monitoring</title> "
    "<description>Long term data on climate change effects</description>"
)


def matches(query: str) -> bool:
    return H._matches_advanced_filter_text(TEXT, query)


TRUTH_TABLE = {
        "wadden AND bird": True,
        "wadden AND zzzz": False,
        "zzzz AND wadden": False,
        "wadden and bird": True,                 # operators are case-insensitive
        "wadden bird": True,                     # adjacent terms = AND
        "wadden zzzz": False,
        "wadden AND climate change": True,       # unquoted multi-word = AND of words
        "wadden AND climate zzzz": False,
        '"climate change" AND wadden': True,
        '"change climate" AND wadden': False,    # quoted = exact phrase
        "wadden NOT bird": False,                # binary NOT = AND NOT
        "wadden NOT zzzz": True,
        "wadden AND NOT bird": False,
        "NOT zzzz": True,
        "(wadden AND zzzz) OR bird": True,
        "wadden AND (bird OR zzzz)": True,
        "wadden AND (zzzz OR yyyy)": False,
        "zzzz OR yyyy OR sea": True,
        "wadden AND bird AND climate AND sea": True,
        "wadden AND bird AND climate AND zzzz": False,
        "NOT (wadden AND bird)": False,
}


@pytest.mark.parametrize("query, expected", list(TRUTH_TABLE.items()))
def test_and_or_not_truth_table(query, expected):
    assert matches(query) is expected


def test_precedence_not_over_and_over_or():
    assert matches("zzzz AND yyyy OR wadden") is True    # (zzzz AND yyyy) OR wadden
    assert matches("wadden OR zzzz AND yyyy") is True    # wadden OR (zzzz AND yyyy)
    assert matches("NOT zzzz AND wadden") is True        # (NOT zzzz) AND wadden
    assert matches("NOT wadden OR zzzz") is False


@pytest.mark.parametrize("bad", ["", "wadden AND", "AND wadden", "wadden AND (bird", "wadden bird )", "()",
                                 "wadden OR OR bird"])
def test_invalid_queries_filter_nothing(bad):
    assert matches(bad) is True


@pytest.mark.parametrize("evil", [
    '"x") or __import__("os").system("true") or ("y"',
    "__import__('os').system('true')",
    "().__class__.__bases__",
    'a\\" and (1)',
])
def test_query_is_never_executed_as_code(evil, monkeypatch):
    # The old evaluator used eval(); these must be inert search terms.
    called = []
    monkeypatch.setattr("os.system", lambda *a: called.append(a) or 0)
    assert matches(evil) in (True, False)  # no exception
    assert called == []                    # and nothing was executed


def test_clause_derivation():
    d = H._advanced_query_clauses
    assert d("wadden AND bird") == [["wadden"], ["bird"]]
    assert d("wadden OR bird") == [["wadden", "bird"]]
    assert d("(wadden OR veluwe) AND bird") == [["wadden", "veluwe"], ["bird"]]
    assert d("wadden AND NOT sediment") == [["wadden"]]
    assert d("wadden NOT sediment") == [["wadden"]]
    assert d("NOT sediment") == []
    assert d("wadden OR NOT sediment") == []           # a match needs no positive term
    assert d("wadden wadden") == [["wadden"]]          # duplicates collapse
    assert d('"salt marsh" AND wadden') == [["salt marsh"], ["wadden"]]
    assert d("") == [] and d("wadden AND") == []       # empty / invalid -> no pre-filter


def test_server_clauses_by_mode():
    basic = {"mode": "basic", "include_terms": ["a", "b"], "exclude_terms": ["x"], "query": ""}
    assert H._server_clauses(basic) == [["a", "b"]]
    assert H._server_clauses({"mode": "basic", "include_terms": []}) == []
    # Advanced mode ignores leftover basic-mode fields.
    adv = {"mode": "advanced", "include_terms": ["leftover"], "exclude_terms": ["x"], "query": "wadden AND bird"}
    assert H._server_clauses(adv) == [["wadden"], ["bird"]]
    assert H._server_clauses(None) == []


def test_prefilter_is_sound_property():
    """
    If the exact query matches a text, the text must also satisfy every derived
    clause (contain at least one of its terms). Otherwise the server-side
    pre-filter would silently drop records the query is supposed to keep.
    """
    rng = random.Random(1234)
    vocab = ["a", "b", "c", "d", "e"]

    def gen(depth=0):
        r = rng.random()
        if depth > 3 or r < 0.35:
            return rng.choice(vocab)
        if r < 0.55:
            return f"NOT {gen(depth + 1)}"
        op = rng.choice(["AND", "OR", ""])
        return f"({gen(depth + 1)} {op} {gen(depth + 1)})"

    for _ in range(4000):
        query = gen()
        clauses = H._advanced_query_clauses(query)
        for _ in range(8):
            words = [w for w in vocab if rng.random() < 0.5]
            text = " ".join(words)
            if H._matches_advanced_filter_text(text, query):
                for clause in clauses:
                    assert any(t in text for t in clause), (query, text, clauses)


def test_protocol_builders():
    assert _build_query(None, None, ["a", "b"]) == '("a" OR "b")'
    assert _build_query(None, None, None, [["wadden"], ["bird", "owl"]]) == '("wadden") AND ("bird" OR "owl")'
    assert _build_query(None, None, ["leftover"], []) == ""             # explicit clauses win
    assert _build_query("2020-01-01", None, None, [["a"]]) == '("a") AND publication_date:[2020-01-01 TO *]'

    assert _describe_constraints(None, None, None, [["wadden"], ["bird", "owl"]]) == (
        "csw:AnyText LIKE '%wadden%' AND (csw:AnyText LIKE '%bird%' OR csw:AnyText LIKE '%owl%')"
    )

    assert _gbif_queries([]) == [""]
    assert _gbif_queries([["wadden"], ["bird"]]) == ["wadden bird"]     # AND-like q
    assert _gbif_queries([["a", "b"], ["c", "d", "e"]]) == ["a", "b"]   # OR-groups -> union of searches
    assert _gbif_queries([["a", "b"], ["c"]]) == ["c"]                  # unit clause is enough

