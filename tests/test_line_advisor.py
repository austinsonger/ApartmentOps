"""Tests for line_advisor.py - intra-building line-substitution advisor.

Imported via the scripts-path hook in tests/conftest.py. No network calls:
every fixture is plain dicts/lists constructed inline, or small YAML files
written into tmp_path for the loader tests and the CLI.
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
from pathlib import Path

import pytest

import line_advisor
from line_advisor import (
    build_lines_index,
    load_all_line_maps,
    load_line_map,
    recent_observations,
    recent_observations_map,
    suggest,
    unit_line,
)

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / ".claude" / "skills" / "apartmentops" / "scripts" / "line_advisor.py"
)

TOWER = "Example Tower 2"

LINE_MAP = {
    "building": TOWER,
    "source": "https://example.com/tower2/floor-plans",
    "lines": {"C": {"orientation": "S", "floors": "5-40"}},
}

# Rich fixture: a C line with four units (3 live, 1 gone-but-remembered) and
# one D-line unit that must never leak in, since "D" is not a confirmed
# line in LINE_MAP.
VERIFIED_UNITS = [
    {"unit_id": "tower2-6c", "building": TOWER, "unit": "6C", "floor": 6, "price": 2350, "live": True},
    {"unit_id": "tower2-12c", "building": TOWER, "unit": "12C", "floor": 12, "price": 2290, "live": True},
    # deliberately uses contracts.md's real field name to prove _price_of's
    # tolerant lookup, not the ticket-prose "price" key
    {"unit_id": "tower2-18c", "building": TOWER, "unit": "18C", "floor": 18, "rent_verified": 2600, "live": True},
    {"unit_id": "tower2-24c", "building": TOWER, "unit": "24C", "floor": 24, "price": 2600, "live": False},
    {"unit_id": "tower2-6d", "building": TOWER, "unit": "6D", "floor": 6, "price": 2500, "live": True},
    {"unit_id": "other-12c", "building": "Other Tower", "unit": "12C", "floor": 12, "price": 1000, "live": True},
]

TRAJECTORIES = {
    "tower2-6c": [
        {"price": 2300, "status": "live", "observed_at": "2026-01-01T00:00:00+00:00"},
        {"price": 2350, "status": "live", "observed_at": "2026-03-01T00:00:00+00:00"},
    ],
    "tower2-12c": [
        {"price": 2400, "status": "live", "observed_at": "2026-01-15T00:00:00+00:00"},
        {"price": None, "status": "gone", "observed_at": "2026-02-15T00:00:00+00:00"},
        {"price": 2290, "status": "live", "observed_at": "2026-04-01T00:00:00+00:00"},
    ],
    "tower2-18c": [
        {"price": 2600, "status": "live", "observed_at": "2026-05-01T00:00:00+00:00"},
    ],
    "tower2-24c": [
        {"price": 2600, "status": "live", "observed_at": "2025-09-01T00:00:00+00:00"},
        {"price": None, "status": "gone", "observed_at": "2025-10-01T00:00:00+00:00"},
    ],
}


# --------------------------------------------------------------------------
# unit_line
# --------------------------------------------------------------------------


def test_unit_line_trailing_letter_confirmed_by_map():
    assert unit_line("18C", LINE_MAP) == "C"


def test_unit_line_trailing_letter_not_confirmed_returns_none():
    assert unit_line("18D", LINE_MAP) is None  # D is not in LINE_MAP["lines"]


def test_unit_line_no_trailing_letter_returns_none():
    assert unit_line("504", LINE_MAP) is None


def test_unit_line_no_line_map_returns_none():
    assert unit_line("18C", None) is None
    assert unit_line("18C", {}) is None


def test_unit_line_empty_unit_string_returns_none():
    assert unit_line("", LINE_MAP) is None
    assert unit_line(None, LINE_MAP) is None


def test_unit_line_is_case_insensitive_but_returns_canonical_key():
    lm = {"lines": {"C": {}}}
    assert unit_line("18c", lm) == "C"


def test_unit_line_hyphenated_unit_resolves_trailing_letter():
    assert unit_line("12-PH-C", LINE_MAP) == "C"


def test_unit_line_explicit_override_used_when_no_trailing_letter():
    lm = {"lines": {"C": {}}, "units": {"PH1": "C"}}
    assert unit_line("PH1", lm) == "C"


def test_unit_line_explicit_override_ignored_if_line_not_confirmed():
    lm = {"lines": {"C": {}}, "units": {"PH1": "Z"}}
    assert unit_line("PH1", lm) is None


def test_unit_line_explicit_override_takes_precedence_over_trailing_letter():
    # unit ends in "C" but the map explicitly overrides it to "D"
    lm = {"lines": {"C": {}, "D": {}}, "units": {"7C": "D"}}
    assert unit_line("7C", lm) == "D"


# --------------------------------------------------------------------------
# suggest() - siblings
# --------------------------------------------------------------------------


def test_suggest_no_line_map_returns_empty_silently():
    candidate = {"unit_id": "tower2-18c", "building": TOWER, "unit": "18C", "price": 2600}
    result = suggest(candidate, VERIFIED_UNITS, None, TRAJECTORIES)
    assert result == {"siblings": [], "line_history": None}


def test_suggest_unresolvable_unit_returns_empty_silently():
    candidate = {"unit_id": "tower2-504", "building": TOWER, "unit": "504", "price": 2100}
    result = suggest(candidate, VERIFIED_UNITS, LINE_MAP, TRAJECTORIES)
    assert result == {"siblings": [], "line_history": None}


def test_suggest_lists_same_line_siblings_with_correct_signed_deltas():
    # AC: fixture building with a line map and multiple live same-line
    # units - each card lists the others with correct floor and price
    # deltas.
    candidate = {"unit_id": "tower2-18c", "building": TOWER, "unit": "18C", "price": 2600}
    result = suggest(candidate, VERIFIED_UNITS, LINE_MAP, TRAJECTORIES)

    by_unit = {s["unit_id"]: s for s in result["siblings"]}
    assert set(by_unit) == {"tower2-6c", "tower2-12c", "tower2-24c"}

    assert by_unit["tower2-12c"]["floor_delta"] == -6
    assert by_unit["tower2-12c"]["price_delta"] == -310
    assert by_unit["tower2-12c"]["live"] is True
    assert by_unit["tower2-12c"]["same_line"] is True

    assert by_unit["tower2-6c"]["floor_delta"] == -12
    assert by_unit["tower2-6c"]["price_delta"] == -250

    assert by_unit["tower2-24c"]["floor_delta"] == 6
    assert by_unit["tower2-24c"]["price_delta"] == 0
    assert by_unit["tower2-24c"]["live"] is False


def test_suggest_is_symmetric_between_two_live_siblings():
    # AC: two live same-line units each list the other with correct,
    # sign-consistent floor and price deltas.
    candidate_12c = {"unit_id": "tower2-12c", "building": TOWER, "unit": "12C", "price": 2290}
    result_12c = suggest(candidate_12c, VERIFIED_UNITS, LINE_MAP, TRAJECTORIES)
    by_unit = {s["unit_id"]: s for s in result_12c["siblings"]}
    assert by_unit["tower2-18c"]["floor_delta"] == 6
    assert by_unit["tower2-18c"]["price_delta"] == 310


def test_suggest_never_includes_candidate_itself():
    candidate = {"unit_id": "tower2-18c", "building": TOWER, "unit": "18C", "price": 2600}
    result = suggest(candidate, VERIFIED_UNITS, LINE_MAP, TRAJECTORIES)
    assert "tower2-18c" not in {s["unit_id"] for s in result["siblings"]}


def test_suggest_excludes_units_in_a_different_building():
    candidate = {"unit_id": "tower2-18c", "building": TOWER, "unit": "18C", "price": 2600}
    result = suggest(candidate, VERIFIED_UNITS, LINE_MAP, TRAJECTORIES)
    assert "other-12c" not in {s["unit_id"] for s in result["siblings"]}


def test_suggest_excludes_units_in_an_unconfirmed_line():
    candidate = {"unit_id": "tower2-18c", "building": TOWER, "unit": "18C", "price": 2600}
    result = suggest(candidate, VERIFIED_UNITS, LINE_MAP, TRAJECTORIES)
    assert "tower2-6d" not in {s["unit_id"] for s in result["siblings"]}


def test_suggest_ranks_live_cheaper_siblings_first():
    candidate = {"unit_id": "tower2-18c", "building": TOWER, "unit": "18C", "price": 2600}
    result = suggest(candidate, VERIFIED_UNITS, LINE_MAP, TRAJECTORIES)
    order = [s["unit_id"] for s in result["siblings"]]
    # cheapest live sibling first, then the next-cheapest live sibling,
    # then the non-live (gone) sibling last
    assert order == ["tower2-12c", "tower2-6c", "tower2-24c"]


def test_suggest_floor_delta_and_price_delta_none_when_unknown_never_fabricated():
    verified = [
        {"unit_id": "x-1", "building": "X", "unit": "5C", "floor": 5, "price": 1000, "live": True},
        # sibling with no recorded floor and no recorded price
        {"unit_id": "x-2", "building": "X", "unit": "9C", "live": True},
    ]
    lm = {"building": "X", "source": "https://example.com/x", "lines": {"C": {}}}
    candidate = {"unit_id": "x-1", "building": "X", "unit": "5C", "price": 1000}
    result = suggest(candidate, verified, lm, {})
    sib = result["siblings"][0]
    assert sib["unit_id"] == "x-2"
    assert sib["floor_delta"] is None
    assert sib["price_delta"] is None


# --------------------------------------------------------------------------
# suggest() - line_history
# --------------------------------------------------------------------------


def test_suggest_line_history_reproduces_hand_computed_result():
    # AC: the line pricing summary reproduces a hand-computed result from
    # trajectory fixtures.
    #
    # Hand computation across the whole C line (6c, 12c, 18c, 24c):
    #   6c:  live, live                       -> 1 stint
    #   12c: live, gone, live                 -> 2 stints
    #   18c: live                             -> 1 stint
    #   24c: live, gone                       -> 1 stint
    #   total times_listed = 1 + 2 + 1 + 1 = 5
    #   priced observations: 2300,2350,2400,2290,2600,2600
    #   price_min = 2290, price_max = 2600
    candidate = {"unit_id": "tower2-18c", "building": TOWER, "unit": "18C", "price": 2600}
    result = suggest(candidate, VERIFIED_UNITS, LINE_MAP, TRAJECTORIES)
    assert result["line_history"] == {
        "times_listed": 5,
        "price_min": 2290,
        "price_max": 2600,
    }


def test_suggest_line_history_none_when_fewer_than_two_priced_observations():
    # AC: a mapped line with insufficient history renders line_history as
    # None (the "n/a (insufficient ledger data)" state is a dashboard
    # rendering choice keyed on this None) while still listing live
    # siblings.
    building = "Small Tower"
    lm = {"building": building, "source": "https://example.com/small", "lines": {"A": {}}}
    verified = [
        {"unit_id": "small-2a", "building": building, "unit": "2A", "floor": 2, "price": 1800, "live": True},
        {"unit_id": "small-8a", "building": building, "unit": "8A", "floor": 8, "price": 1950, "live": True},
    ]
    trajectories = {
        "small-2a": [{"price": 1800, "status": "live", "observed_at": "2026-06-01T00:00:00+00:00"}],
    }
    candidate = {"unit_id": "small-2a", "building": building, "unit": "2A", "price": 1800}
    result = suggest(candidate, verified, lm, trajectories)

    assert result["siblings"] == [
        {"unit_id": "small-8a", "floor_delta": 6, "price_delta": 150, "same_line": True, "live": True}
    ]
    assert result["line_history"] is None


def test_suggest_line_history_none_with_zero_observations():
    candidate = {"unit_id": "tower2-18c", "building": TOWER, "unit": "18C", "price": 2600}
    result = suggest(candidate, VERIFIED_UNITS, LINE_MAP, {})
    assert result["line_history"] is None
    assert result["siblings"] != []  # siblings unaffected by missing trajectories


# --------------------------------------------------------------------------
# recent_observations / recent_observations_map
# --------------------------------------------------------------------------


def test_recent_observations_excludes_entries_outside_window():
    now = datetime.datetime(2026, 7, 15, tzinfo=datetime.timezone.utc)
    obs = [
        {"price": 100, "status": "live", "observed_at": "2025-01-01T00:00:00+00:00"},  # >12mo old
        {"price": 200, "status": "live", "observed_at": "2026-06-01T00:00:00+00:00"},  # recent
    ]
    kept = recent_observations(obs, months=12, now=now)
    assert [o["price"] for o in kept] == [200]


def test_recent_observations_excludes_entries_with_no_observed_at():
    now = datetime.datetime(2026, 7, 15, tzinfo=datetime.timezone.utc)
    obs = [{"price": 100, "status": "live"}]
    assert recent_observations(obs, months=12, now=now) == []


def test_recent_observations_map_applies_per_unit():
    now = datetime.datetime(2026, 7, 15, tzinfo=datetime.timezone.utc)
    trajectories = {
        "u1": [{"price": 100, "status": "live", "observed_at": "2020-01-01T00:00:00+00:00"}],
        "u2": [{"price": 200, "status": "live", "observed_at": "2026-06-01T00:00:00+00:00"}],
    }
    windowed = recent_observations_map(trajectories, months=12, now=now)
    assert windowed["u1"] == []
    assert windowed["u2"] == trajectories["u2"]


# --------------------------------------------------------------------------
# build_lines_index
# --------------------------------------------------------------------------


def test_build_lines_index_omits_buildings_with_no_line_map():
    verified = VERIFIED_UNITS + [
        {"unit_id": "unmapped-5a", "building": "Unmapped Tower", "unit": "5A", "floor": 5, "price": 3000, "live": True},
    ]
    now = datetime.datetime(2026, 7, 15, tzinfo=datetime.timezone.utc)
    index, report = build_lines_index(
        verified, {TOWER: LINE_MAP}, TRAJECTORIES, months=12, now=now
    )
    assert "unmapped-5a" not in index
    # VERIFIED_UNITS already contributes "other-12c" in an unmapped
    # building; adding unmapped-5a brings the count to 2.
    assert report["no_line_map"] == 2


def test_build_lines_index_omits_unparsed_units_and_counts_them():
    verified = VERIFIED_UNITS + [
        {"unit_id": "tower2-504", "building": TOWER, "unit": "504", "floor": 5, "price": 2100, "live": True},
    ]
    now = datetime.datetime(2026, 7, 15, tzinfo=datetime.timezone.utc)
    index, report = build_lines_index(
        verified, {TOWER: LINE_MAP}, TRAJECTORIES, months=12, now=now
    )
    assert "tower2-504" not in index
    # VERIFIED_UNITS already contributes "tower2-6d" (line D, unconfirmed);
    # adding tower2-504 (no trailing letter) brings the count to 2.
    assert report["unparsed_unit"] == 2


def test_build_lines_index_excludes_non_live_units_as_candidates():
    now = datetime.datetime(2026, 7, 15, tzinfo=datetime.timezone.utc)
    index, report = build_lines_index(
        VERIFIED_UNITS, {TOWER: LINE_MAP}, TRAJECTORIES, months=12, now=now
    )
    # tower2-24c is live: False - it must never get its own index entry,
    # even though it legitimately appears inside OTHER units' siblings
    assert "tower2-24c" not in index
    assert "tower2-24c" in {s["unit_id"] for s in index["tower2-18c"]["siblings"]}


def test_build_lines_index_report_partitions_live_candidates():
    verified = VERIFIED_UNITS + [
        {"unit_id": "unmapped-5a", "building": "Unmapped Tower", "unit": "5A", "floor": 5, "price": 3000, "live": True},
        {"unit_id": "tower2-504", "building": TOWER, "unit": "504", "floor": 5, "price": 2100, "live": True},
    ]
    now = datetime.datetime(2026, 7, 15, tzinfo=datetime.timezone.utc)
    index, report = build_lines_index(
        verified, {TOWER: LINE_MAP}, TRAJECTORIES, months=12, now=now
    )
    assert report["live_candidates"] == report["no_line_map"] + report["unparsed_unit"] + report["included"]
    assert report["included"] == len(index)


def test_build_lines_index_applies_the_trailing_window_before_suggesting():
    # A trajectory point far outside a 1-month window must not count
    # toward line_history even though it would inside the 12-month default.
    now = datetime.datetime(2026, 7, 15, tzinfo=datetime.timezone.utc)
    index_12mo, _ = build_lines_index(VERIFIED_UNITS, {TOWER: LINE_MAP}, TRAJECTORIES, months=12, now=now)
    index_1mo, _ = build_lines_index(VERIFIED_UNITS, {TOWER: LINE_MAP}, TRAJECTORIES, months=1, now=now)
    # with only a 1-month window, none of the fixture's observations (all
    # from January-May 2026) are within [2026-06-15, 2026-07-15]
    assert index_1mo["tower2-18c"]["line_history"] is None
    assert index_12mo["tower2-18c"]["line_history"] is not None


# --------------------------------------------------------------------------
# YAML line-map loading
# --------------------------------------------------------------------------


def test_load_line_map_reads_yaml_file(tmp_path):
    yaml_text = """
building: "Example Tower 2"
source: "https://example.com/tower2/floor-plans"
lines:
  C:
    orientation: S
    floors: "5-40"
units:
  "3201": "C"
"""
    p = tmp_path / "example-tower-2.yml"
    p.write_text(yaml_text)
    line_map = load_line_map(p)
    assert line_map["building"] == "Example Tower 2"
    assert line_map["lines"]["C"]["orientation"] == "S"
    assert line_map["units"]["3201"] == "C"


def test_load_all_line_maps_keys_by_building_field_not_filename(tmp_path):
    (tmp_path / "some-slug.yml").write_text(
        'building: "Example Tower 2"\nsource: "https://example.com/x"\nlines:\n  C: {}\n'
    )
    maps = load_all_line_maps(tmp_path)
    assert "Example Tower 2" in maps
    assert "some-slug" not in maps


def test_load_all_line_maps_skips_files_with_no_building_field(tmp_path):
    (tmp_path / "broken.yml").write_text("lines:\n  C: {}\n")
    maps = load_all_line_maps(tmp_path)
    assert maps == {}


def test_missing_pyyaml_raises_clear_install_hint(monkeypatch):
    monkeypatch.setattr(line_advisor, "yaml", None)
    with pytest.raises(ImportError, match="pip install PyYAML"):
        line_advisor._require_yaml()


# --------------------------------------------------------------------------
# Zero network requests (static guardrail)
# --------------------------------------------------------------------------


def test_module_source_contains_no_network_calls():
    source = SCRIPT.read_text()
    forbidden = ["urllib", "requests", "http.client", "socket.", "playwright"]
    for token in forbidden:
        assert token not in source, f"unexpected network-capable token: {token}"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_build_writes_lines_index_and_report(tmp_path):
    verified_path = tmp_path / "verified.json"
    verified_path.write_text(json.dumps(VERIFIED_UNITS))

    lines_dir = tmp_path / "lines"
    lines_dir.mkdir()
    (lines_dir / "example-tower-2.yml").write_text(
        'building: "Example Tower 2"\n'
        'source: "https://example.com/tower2/floor-plans"\n'
        "lines:\n  C:\n    orientation: S\n    floors: \"5-40\"\n"
    )

    trajectories_path = tmp_path / "trajectories.json"
    trajectories_path.write_text(json.dumps(TRAJECTORIES))

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "build", str(verified_path), str(lines_dir), str(trajectories_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(proc.stdout)
    assert "tower2-18c" in payload["lines"]
    assert payload["lines"]["tower2-18c"]["line_history"]["times_listed"] == 5
    assert payload["report"]["included"] >= 1


def test_cli_wrong_usage_prints_usage_and_exits_2():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "usage" in proc.stderr.lower()
