"""Tests for gates.py: provenance helpers, tri-state hard gates, and the
structurally-checkable subset of anti-fabrication grading. No network
calls, no filesystem beyond tmp_path in the CLI test.
"""

from __future__ import annotations

import json

import pytest

import gates


# ---------------------------------------------------------------------------
# field_value / field_status
# ---------------------------------------------------------------------------

def test_field_value_legacy_scalar():
    assert gates.field_value(2450) == 2450
    assert gates.field_value(None) is None
    assert gates.field_value(True) is True


def test_field_status_legacy_scalar():
    assert gates.field_status(2450) == gates.FACT
    assert gates.field_status(None) == gates.MISSING
    assert gates.field_status(False) == gates.FACT  # falsy but not None


def test_field_value_and_status_provenance_dict():
    field = {"value": 2450, "status": "FACT", "source": "https://x", "evidence": "shot.png"}
    assert gates.field_value(field) == 2450
    assert gates.field_status(field) == "FACT"


def test_field_status_unknown_status_raises():
    with pytest.raises(ValueError):
        gates.field_status({"value": 1, "status": "BOGUS"})


def test_field_status_missing_status_key_falls_back_to_legacy():
    # a dict without "status" is treated as a legacy scalar wrapper - odd,
    # but should never raise, and its whole self is the "value".
    field = {"value": 1}
    assert gates.field_status(field) == gates.FACT


# ---------------------------------------------------------------------------
# normalize_field
# ---------------------------------------------------------------------------

def test_normalize_field_legacy_nonnull_becomes_fact_source_null():
    out = gates.normalize_field(2450)
    assert out == {"value": 2450, "status": "FACT", "source": None, "evidence": None}


def test_normalize_field_legacy_null_becomes_missing():
    out = gates.normalize_field(None)
    assert out == {"value": None, "status": "MISSING", "source": None, "evidence": None}


def test_normalize_field_inferred_below_threshold_downgrades_to_missing():
    field = {"value": "SW corner", "status": "INFERRED", "confidence": 0.25,
              "source": "https://x", "evidence": "reasoning text"}
    out = gates.normalize_field(field)
    assert out["status"] == "MISSING"
    assert out["value"] is None


def test_normalize_field_inferred_at_exactly_threshold_stays_inferred():
    field = {"value": "SW corner", "status": "INFERRED", "confidence": 0.3,
              "source": "https://x", "evidence": "reasoning text"}
    out = gates.normalize_field(field)
    assert out["status"] == "INFERRED"
    assert out["value"] == "SW corner"


def test_normalize_field_inferred_missing_confidence_downgrades():
    field = {"value": "SW corner", "status": "INFERRED", "source": "https://x"}
    out = gates.normalize_field(field)
    assert out["status"] == "MISSING"
    assert out["value"] is None


def test_normalize_field_inferred_above_threshold_keeps_value():
    field = {"value": "SW corner", "status": "INFERRED", "confidence": 0.75,
              "source": "https://x", "evidence": "reasoning"}
    out = gates.normalize_field(field)
    assert out["status"] == "INFERRED"
    assert out["value"] == "SW corner"


def test_normalize_field_missing_forces_value_null_even_if_stray_value_present():
    field = {"value": "should be discarded", "status": "MISSING"}
    out = gates.normalize_field(field)
    assert out["value"] is None
    assert out["status"] == "MISSING"


def test_normalize_field_conflict_forces_null_value_keeps_conflicts():
    field = {
        "value": "should not survive",
        "status": "CONFLICT",
        "conflicts": [
            {"value": 2450, "source": "https://a", "evidence": "shot-a.png"},
            {"value": 2500, "source": "https://b", "evidence": "shot-b.png"},
        ],
    }
    out = gates.normalize_field(field)
    assert out["value"] is None
    assert out["status"] == "CONFLICT"
    assert len(out["conflicts"]) == 2


def test_normalize_field_unknown_status_raises():
    with pytest.raises(ValueError):
        gates.normalize_field({"value": 1, "status": "NOPE"})


# ---------------------------------------------------------------------------
# evaluate_gates
# ---------------------------------------------------------------------------

def test_evaluate_gates_hard_fact_pass():
    config_gates = {"in_unit_laundry": {"field": "in_unit_laundry", "op": "eq",
                                          "value": True, "mode": "hard"}}
    unit = {"in_unit_laundry": {"value": True, "status": "FACT", "source": "https://x",
                                  "evidence": "shot.png"}}
    result = gates.evaluate_gates(config_gates, unit)
    assert result == {"in_unit_laundry": "PASS"}


def test_evaluate_gates_hard_fact_fail_excludes():
    config_gates = {"rent_max": {"field": "rent_verified", "op": "lte",
                                   "value": 4900, "mode": "hard"}}
    unit = {"rent_verified": {"value": 5200, "status": "FACT", "source": "https://x",
                                "evidence": "shot.png"}}
    result = gates.evaluate_gates(config_gates, unit)
    assert result == {"rent_max": "FAIL"}


def test_evaluate_gates_missing_field_is_unknown():
    config_gates = {"in_unit_laundry": {"field": "in_unit_laundry", "op": "eq",
                                          "value": True, "mode": "hard"}}
    unit = {"in_unit_laundry": None}
    result = gates.evaluate_gates(config_gates, unit)
    assert result == {"in_unit_laundry": "UNKNOWN"}


def test_evaluate_gates_field_absent_from_unit_is_unknown():
    config_gates = {"in_unit_laundry": {"field": "in_unit_laundry", "op": "eq",
                                          "value": True, "mode": "hard"}}
    result = gates.evaluate_gates(config_gates, {})
    assert result == {"in_unit_laundry": "UNKNOWN"}


def test_evaluate_gates_inferred_field_is_unknown_regardless_of_confidence():
    config_gates = {"floor_min": {"field": "floor", "op": "gte", "value": 10, "mode": "hard"}}
    unit = {"floor": {"value": 34, "status": "INFERRED", "confidence": 0.95,
                        "source": "https://x", "evidence": "reasoning"}}
    result = gates.evaluate_gates(config_gates, unit)
    assert result == {"floor_min": "UNKNOWN"}


def test_evaluate_gates_conflict_field_is_unknown_not_either_value():
    config_gates = {"rent_max": {"field": "rent_verified", "op": "lte",
                                   "value": 4900, "mode": "hard"}}
    unit = {"rent_verified": {
        "value": None, "status": "CONFLICT",
        "conflicts": [{"value": 4500, "source": "https://a", "evidence": "a.png"},
                       {"value": 5300, "source": "https://b", "evidence": "b.png"}],
    }}
    result = gates.evaluate_gates(config_gates, unit)
    assert result == {"rent_max": "UNKNOWN"}


def test_evaluate_gates_bonus_gate_never_fails():
    config_gates = {"floor_min": {"field": "floor", "op": "gte", "value": 10, "mode": "bonus"}}
    unit = {"floor": {"value": 3, "status": "FACT", "source": "https://x", "evidence": "shot.png"}}
    result = gates.evaluate_gates(config_gates, unit)
    assert result == {"floor_min": "UNKNOWN"}  # never FAIL, even though 3 < 10


def test_evaluate_gates_bonus_gate_can_still_pass():
    config_gates = {"floor_min": {"field": "floor", "op": "gte", "value": 10, "mode": "bonus"}}
    unit = {"floor": {"value": 34, "status": "FACT", "source": "https://x", "evidence": "shot.png"}}
    result = gates.evaluate_gates(config_gates, unit)
    assert result == {"floor_min": "PASS"}


def test_evaluate_gates_default_field_name_is_gate_name():
    config_gates = {"pets": {"op": "eq", "value": "cat", "mode": "hard"}}
    unit = {"pets": {"value": "cat", "status": "FACT", "source": "https://x", "evidence": "e"}}
    result = gates.evaluate_gates(config_gates, unit)
    assert result == {"pets": "PASS"}


def test_evaluate_gates_incomparable_types_is_unknown_not_a_crash():
    config_gates = {"floor_min": {"field": "floor", "op": "gte", "value": 10, "mode": "hard"}}
    unit = {"floor": {"value": "penthouse", "status": "FACT", "source": "https://x", "evidence": "e"}}
    result = gates.evaluate_gates(config_gates, unit)
    assert result == {"floor_min": "UNKNOWN"}


def test_evaluate_gates_multiple_gates_independent():
    config_gates = {
        "in_unit_laundry": {"field": "in_unit_laundry", "op": "eq", "value": True, "mode": "hard"},
        "rent_max": {"field": "rent_verified", "op": "lte", "value": 4900, "mode": "hard"},
    }
    unit = {
        "in_unit_laundry": None,
        "rent_verified": {"value": 4700, "status": "FACT", "source": "https://x", "evidence": "e"},
    }
    result = gates.evaluate_gates(config_gates, unit)
    assert result == {"in_unit_laundry": "UNKNOWN", "rent_max": "PASS"}


# ---------------------------------------------------------------------------
# verify_checklist / tour_now_blocked
# ---------------------------------------------------------------------------

def test_verify_checklist_one_item_per_unknown_gate_with_deep_link():
    gate_results = {"in_unit_laundry": "UNKNOWN", "rent_max": "PASS"}
    unit = {"unit_deep_link": "https://platform.example/unit/123"}
    checklist = gates.verify_checklist(gate_results, unit)
    assert len(checklist) == 1
    assert "in unit laundry" in checklist[0]
    assert "https://platform.example/unit/123" in checklist[0]


def test_verify_checklist_no_deep_link_still_produces_item():
    gate_results = {"rent_max": "UNKNOWN"}
    checklist = gates.verify_checklist(gate_results, {})
    assert len(checklist) == 1
    assert "no deep link on file" in checklist[0]


def test_verify_checklist_ignores_pass_and_fail():
    gate_results = {"a": "PASS", "b": "FAIL"}
    assert gates.verify_checklist(gate_results, {}) == []


def test_verify_checklist_falls_back_to_url_field():
    gate_results = {"rent_max": "UNKNOWN"}
    unit = {"url": "https://platform.example/listing"}
    checklist = gates.verify_checklist(gate_results, unit)
    assert "https://platform.example/listing" in checklist[0]


def test_tour_now_blocked_true_on_unknown():
    assert gates.tour_now_blocked({"a": "PASS", "b": "UNKNOWN"}) is True


def test_tour_now_blocked_true_on_fail():
    assert gates.tour_now_blocked({"a": "PASS", "b": "FAIL"}) is True


def test_tour_now_blocked_false_when_all_pass():
    assert gates.tour_now_blocked({"a": "PASS", "b": "PASS"}) is False


def test_tour_now_blocked_false_on_empty_gates():
    assert gates.tour_now_blocked({}) is False


# ---------------------------------------------------------------------------
# grade_fields
# ---------------------------------------------------------------------------

def test_grade_fields_value_without_provenance():
    data = {"price": {"value": 2450, "status": "FACT", "source": None, "evidence": None}}
    report = gates.grade_fields(data)
    rule_ids = [a["rule_id"] for a in report["autofails"]]
    assert "value-without-provenance" in rule_ids


def test_grade_fields_fact_with_source_is_clean():
    data = {"price": {"value": 2450, "status": "FACT", "source": "https://x", "evidence": "shot.png"}}
    report = gates.grade_fields(data)
    assert report["autofails"] == []


def test_grade_fields_missing_value_is_never_flagged_for_provenance():
    data = {"price": {"value": None, "status": "MISSING", "source": None, "evidence": None}}
    report = gates.grade_fields(data)
    assert report["autofails"] == []


def test_grade_fields_placeholder_pattern_nested():
    data = {"unit": {"notes": "TODO: fill in the real notes here"}}
    report = gates.grade_fields(data)
    rule_ids = [a["rule_id"] for a in report["autofails"]]
    assert "placeholder-left-in-output" in rule_ids


def test_grade_fields_placeholder_pattern_in_list():
    data = {"units": [{"notes": "fine"}, {"notes": "contact us at test@example.com"}]}
    report = gates.grade_fields(data)
    rule_ids = [a["rule_id"] for a in report["autofails"]]
    assert "placeholder-left-in-output" in rule_ids


def test_grade_fields_grade_without_source():
    data = {"safety": {"grade": "B", "summary": "fine"}}
    report = gates.grade_fields(data)
    rule_ids = [a["rule_id"] for a in report["autofails"]]
    assert "grade-without-source" in rule_ids


def test_grade_fields_grade_with_sources_is_clean():
    data = {"safety": {"grade": "B", "summary": "fine", "sources": ["https://x"]}}
    report = gates.grade_fields(data)
    assert report["autofails"] == []


def test_grade_fields_price_not_number():
    data = {"unit": {"rent_verified": "$2,450"}}
    report = gates.grade_fields(data)
    rule_ids = [a["rule_id"] for a in report["autofails"]]
    assert "price-not-number" in rule_ids


def test_grade_fields_price_as_number_is_clean():
    data = {"unit": {"rent_verified": 2450}}
    report = gates.grade_fields(data)
    assert report["autofails"] == []


def test_grade_fields_price_not_number_inside_provenance_dict():
    # Regression: under the provenance schema (ticket 4 of this epic), every
    # price/rent field is {value, status, source, evidence, confidence}, not
    # a bare scalar. The string-price check must look inside that dict at
    # its carried "value", not just at the immediate field value (which is
    # always a dict once provenance-wrapped, so a naive isinstance(..., str)
    # check would never fire against real verified.json data).
    data = {"price": {"value": "$2,450", "status": "FACT",
                        "source": "https://x", "evidence": "s.png"}}
    report = gates.grade_fields(data)
    rule_ids = [a["rule_id"] for a in report["autofails"]]
    assert "price-not-number" in rule_ids


def test_grade_fields_price_as_number_inside_provenance_dict_is_clean():
    data = {"price": {"value": 2450, "status": "FACT",
                        "source": "https://x", "evidence": "s.png"}}
    report = gates.grade_fields(data)
    assert report["autofails"] == []


def test_grade_fields_unverified_marked_live():
    data = {"unit": {"live": True, "verified_at": None, "screenshot": None}}
    report = gates.grade_fields(data)
    rule_ids = [a["rule_id"] for a in report["autofails"]]
    assert "unverified-marked-live" in rule_ids


def test_grade_fields_verified_live_is_clean():
    data = {"unit": {"live": True, "verified_at": "2026-07-15T14:00:00-04:00",
                       "screenshot": "shots/unit.png"}}
    report = gates.grade_fields(data)
    assert report["autofails"] == []


def test_grade_fields_respects_constraints_autofail_filter():
    data = {"price": {"value": 2450, "status": "FACT", "source": None, "evidence": None}}
    constraints = {
        "version": 1,
        "rules": [{"id": "value-without-provenance", "description": "x", "autofail": False}],
    }
    report = gates.grade_fields(data, constraints)
    assert report["autofails"] == []  # rule present but autofail: false -> filtered out


def test_grade_fields_counts_dict_records_visited():
    data = {"a": {"b": {"c": 1}}}
    report = gates.grade_fields(data)
    assert report["fields_graded"] == 3  # {"a":...}, {"b":...}, {"c":...}? c is int not dict
    # only "a"'s value and "b"'s value are dicts (2), plus the top-level
    # dict itself (1) = 3 records visited.


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_end_to_end(tmp_path, capsys):
    unit_path = tmp_path / "unit.json"
    unit_path.write_text(json.dumps({
        "in_unit_laundry": None,
        "rent_verified": {"value": 4700, "status": "FACT", "source": "https://x", "evidence": "e"},
        "unit_deep_link": "https://platform.example/unit/1",
    }))
    config_gates_path = tmp_path / "config_gates.json"
    config_gates_path.write_text(json.dumps({
        "in_unit_laundry": {"field": "in_unit_laundry", "op": "eq", "value": True, "mode": "hard"},
        "rent_max": {"field": "rent_verified", "op": "lte", "value": 4900, "mode": "hard"},
    }))

    rc = gates.main([str(unit_path), str(config_gates_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["gates"] == {"in_unit_laundry": "UNKNOWN", "rent_max": "PASS"}
    assert out["tour_now_blocked"] is True
    assert len(out["verify_checklist"]) == 1


def test_cli_wrong_arg_count(capsys):
    rc = gates.main(["only_one.json"])
    assert rc == 2
    assert "usage" in capsys.readouterr().err.lower()
