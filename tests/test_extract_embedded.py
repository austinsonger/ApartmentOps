"""Tests for extract_embedded.py: payload discovery, path resolution, and
field extraction. No network calls - everything runs against saved fixture
HTML under tests/fixtures/e1/.
"""

from __future__ import annotations

import json
import pathlib

import pytest

import extract_embedded as ee

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "e1"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# find_embedded_payloads
# ---------------------------------------------------------------------------

def test_finds_next_data_payload():
    payloads = ee.find_embedded_payloads(_read("next_data.html"))
    kinds = [p["kind"] for p in payloads]
    assert kinds == ["next_data"]
    assert payloads[0]["data"]["props"]["pageProps"]["listing"]["price"] == 2650


def test_finds_ld_json_payload():
    payloads = ee.find_embedded_payloads(_read("ld_json.html"))
    kinds = [p["kind"] for p in payloads]
    assert kinds == ["ld_json"]
    assert payloads[0]["data"]["numberOfBedrooms"] == 2


def test_finds_window_state_payloads():
    payloads = ee.find_embedded_payloads(_read("window_state.html"))
    window_payloads = [p for p in payloads if p["kind"] == "window_state"]
    assert len(window_payloads) == 2
    by_var = {p["var"]: p["data"] for p in window_payloads}
    assert by_var["__INITIAL_STATE__"]["listing"]["price"] == 4200
    assert by_var["__ANALYTICS__"]["pageType"] == "listing"


def test_finds_window_state_payloads_with_bare_variable_names():
    # Regression: the window-state assignment regex must not require the
    # variable name to be wrapped in leading/trailing underscores (e.g.
    # "__INITIAL_STATE__"). A plain identifier like APP_STATE or
    # dataLayerState is equally legal JS and must be detected too - a
    # platform using one should never falsely probe as payload: none.
    payloads = ee.find_embedded_payloads(_read("window_state_bare_names.html"))
    window_payloads = [p for p in payloads if p["kind"] == "window_state"]
    assert len(window_payloads) == 2
    by_var = {p["var"]: p["data"] for p in window_payloads}
    assert by_var["APP_STATE"]["listing"]["price"] == 3350
    assert by_var["dataLayerState"]["pageType"] == "listing"


def test_window_state_non_json_assignment_is_not_a_false_positive():
    # window.location = location.href is a real-world assignment pattern
    # that must never be mistaken for a JSON window-state blob - the JSON
    # raw_decode step filters it out regardless of variable-name shape.
    html = _read("window_state_bare_names.html")
    payloads = ee.find_embedded_payloads(html)
    assert all(p.get("var") != "location" for p in payloads)


def test_no_payload_page_yields_empty_list():
    payloads = ee.find_embedded_payloads(_read("no_payload.html"))
    assert payloads == []


def test_malformed_next_data_is_skipped_not_raised():
    # Truncated JSON in the __NEXT_DATA__ tag must never raise and must
    # never be guessed at - it is simply absent from the result.
    payloads = ee.find_embedded_payloads(_read("malformed_next_data.html"))
    assert payloads == []


def test_external_script_src_is_ignored_for_window_state():
    html = _read("window_state.html")
    assert "vendor-bundle.js" in html  # sanity: fixture really has a src= script
    payloads = ee.find_embedded_payloads(html)
    # only the two inline window.X = {...} assignments should surface
    assert len(payloads) == 2


# ---------------------------------------------------------------------------
# resolve_path
# ---------------------------------------------------------------------------

def test_resolve_path_nested_dict():
    data = {"a": {"b": {"c": 42}}}
    found, value = ee.resolve_path(data, "a.b.c")
    assert (found, value) == (True, 42)


def test_resolve_path_list_index():
    data = {"items": [{"price": 100}, {"price": 200}]}
    found, value = ee.resolve_path(data, "items[1].price")
    assert (found, value) == (True, 200)


def test_resolve_path_bare_numeric_segment_is_index():
    data = {"items": [{"price": 100}, {"price": 200}]}
    found, value = ee.resolve_path(data, "items.0.price")
    assert (found, value) == (True, 100)


def test_resolve_path_wildcard_over_list_returns_first_match():
    data = {"items": [{"id": "A"}, {"id": "B", "price": 900}, {"id": "C", "price": 999}]}
    found, value = ee.resolve_path(data, "items.*.price")
    # first item lacks "price" entirely, so the wildcard must skip past it
    assert (found, value) == (True, 900)


def test_resolve_path_wildcard_bracket_form():
    data = {"items": [{"id": "A"}, {"id": "B", "price": 900}]}
    found, value = ee.resolve_path(data, "items[*].price")
    assert (found, value) == (True, 900)


def test_resolve_path_wildcard_over_dict_values():
    data = {"units": {"u1": {"price": 111}, "u2": {"price": 222}}}
    found, value = ee.resolve_path(data, "units.*.price")
    # dict iteration order is insertion order in py3.7+, so the wildcard
    # should hit "u1" first.
    assert (found, value) == (True, 111)


def test_resolve_path_missing_returns_false_none():
    data = {"a": {"b": 1}}
    found, value = ee.resolve_path(data, "a.z")
    assert (found, value) == (False, None)


def test_resolve_path_index_out_of_range_is_missing():
    data = {"items": [1, 2]}
    found, value = ee.resolve_path(data, "items[5]")
    assert (found, value) == (False, None)


def test_resolve_path_wrong_shape_is_missing():
    data = {"a": "not a dict"}
    found, value = ee.resolve_path(data, "a.b")
    assert (found, value) == (False, None)


def test_resolve_path_malformed_segment_raises_value_error():
    with pytest.raises(ValueError):
        ee.resolve_path({}, "a..b")


# ---------------------------------------------------------------------------
# extract_fields
# ---------------------------------------------------------------------------

def test_extract_fields_happy_path_next_data():
    payloads = ee.find_embedded_payloads(_read("next_data.html"))
    spec = {
        "platform": "example_platform",
        "payload": "next_data",
        "fields": {
            "price": "props.pageProps.listing.price",
            "beds": "props.pageProps.listing.beds",
            "unit": "props.pageProps.listing.unitNumber",
        },
    }
    fields = ee.extract_fields(payloads, spec)
    assert fields["price"] == {
        "value": 2650, "method": "embedded:next_data",
        "path": "props.pageProps.listing.price",
    }
    assert fields["beds"]["value"] == 2
    assert fields["unit"]["value"] == "12B"


def test_extract_fields_unresolvable_path_is_missing():
    payloads = ee.find_embedded_payloads(_read("next_data.html"))
    spec = {
        "payload": "next_data",
        "fields": {"nonexistent": "props.pageProps.listing.floorPlanUrl"},
    }
    fields = ee.extract_fields(payloads, spec)
    assert fields["nonexistent"] == {
        "value": None, "method": None,
        "path": "props.pageProps.listing.floorPlanUrl",
    }


def test_extract_fields_no_payloads_all_missing():
    fields = ee.extract_fields([], {"fields": {"price": "a.b.price"}})
    assert fields["price"]["value"] is None
    assert fields["price"]["method"] is None


def test_extract_fields_accepts_paths_alias_key():
    payloads = ee.find_embedded_payloads(_read("next_data.html"))
    spec = {"payload": "next_data", "paths": {"price": "props.pageProps.listing.price"}}
    fields = ee.extract_fields(payloads, spec)
    assert fields["price"]["value"] == 2650
    assert fields["price"]["method"] == "embedded:next_data"


def test_extract_fields_fields_key_wins_over_paths_alias():
    payloads = ee.find_embedded_payloads(_read("next_data.html"))
    spec = {
        "payload": "next_data",
        "fields": {"price": "props.pageProps.listing.price"},
        "paths": {"price": "props.pageProps.listing.beds"},  # would resolve to 2, not used
    }
    fields = ee.extract_fields(payloads, spec)
    assert fields["price"]["value"] == 2650


def test_extract_fields_wildcard_field():
    payloads = ee.find_embedded_payloads(_read("next_data.html"))
    spec = {"payload": "next_data", "fields": {"any_price": "props.pageProps.units.*.price"}}
    fields = ee.extract_fields(payloads, spec)
    assert fields["any_price"]["value"] == 2400  # first unit in the list


def test_extract_fields_falls_back_when_preferred_kind_absent():
    # spec asks for next_data, but only ld_json was found on the page - the
    # field should still resolve against whatever payload is available.
    payloads = ee.find_embedded_payloads(_read("ld_json.html"))
    spec = {"payload": "next_data", "fields": {"beds": "numberOfBedrooms"}}
    fields = ee.extract_fields(payloads, spec)
    assert fields["beds"] == {
        "value": 2, "method": "embedded:ld_json", "path": "numberOfBedrooms",
    }


def test_extract_fields_never_guesses_never_raises_on_no_payload_page():
    payloads = ee.find_embedded_payloads(_read("no_payload.html"))
    assert payloads == []
    spec = {"payload": "none", "fields": {"price": "listing.price"}}
    fields = ee.extract_fields(payloads, spec)
    assert fields == {"price": {"value": None, "method": None, "path": "listing.price"}}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_end_to_end_with_json_spec(tmp_path, capsys):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "platform": "example_platform",
        "payload": "next_data",
        "fields": {"price": "props.pageProps.listing.price"},
    }))
    html_path = FIXTURES / "next_data.html"

    rc = ee.main([str(html_path), str(spec_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["price"]["value"] == 2650
    assert out["price"]["method"] == "embedded:next_data"


def test_cli_end_to_end_with_yaml_spec(tmp_path, capsys):
    yaml = pytest.importorskip("yaml")
    spec_path = tmp_path / "spec.yml"
    spec_path.write_text(
        "platform: example_platform\n"
        "payload: next_data\n"
        "fields:\n"
        "  price: props.pageProps.listing.price\n"
        "  beds: props.pageProps.listing.beds\n"
    )
    html_path = FIXTURES / "next_data.html"

    rc = ee.main([str(html_path), str(spec_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["price"]["value"] == 2650
    assert out["beds"]["value"] == 2


def test_cli_wrong_arg_count_returns_usage_error(capsys):
    rc = ee.main(["only_one_arg.html"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "usage" in err.lower()
