"""Tests for shortlist.py - rows, sync plan, link gate, read-back diff.
No network and no document access anywhere in this file.
"""

from __future__ import annotations

import pytest

import shortlist

COLUMNS = [
    {"field": "num", "label": "#"},
    {"field": "address", "label": "Address"},
    {"field": "name", "label": "Name"},
    {"field": "walkScore", "label": "Walk Score"},
    {"field": "price", "label": "Price"},
    {"field": "sqft", "label": "Sq ft"},
    {"field": "availability", "label": "Available"},
    {"field": "link", "label": "Link"},
    {"field": "review", "label": "Review"},
]


def _unit(building="Tower 2", unit="2207", address="1 Example Ave, Chicago, IL", **kw):
    u = {"building": building, "unit": unit, "address": address, "live": True,
         "rent_verified": 3400, "sqft": 1000, "available": "2026-11-01",
         "unit_deep_link": "https://op.test/unit/2207", "verify_url": "https://op.test/verify",
         "url": "https://op.test/listing"}
    u.update(kw)
    return u


def _row(address="1 Example Ave Unit 2207, Chicago, IL", link="https://op.test/unit/2207"):
    return {"address": address, "link": link, "key": shortlist.norm_address(address)}


def _evidence(**kw):
    e = {"final_url": "https://op.test/unit/2207", "status": 200,
         "page_text_excerpt": "Unit 2207 at 1 Example Ave, Chicago. Available Nov 1.",
         "availability_signal": True, "whole_unit": True, "unit_level": True}
    e.update(kw)
    return e


# ------------------------------------------------------------------ columns

def test_validate_columns_requires_address_and_link():
    shortlist.validate_columns(COLUMNS)
    with pytest.raises(ValueError, match="address"):
        shortlist.validate_columns([c for c in COLUMNS if c["field"] != "address"])
    with pytest.raises(ValueError, match="link"):
        shortlist.validate_columns([c for c in COLUMNS if c["field"] != "link"])


def test_validate_columns_rejects_duplicate_field():
    with pytest.raises(ValueError, match="field 'price'"):
        shortlist.validate_columns(COLUMNS + [{"field": "price", "label": "Rent"}])
    with pytest.raises(ValueError, match="label 'Price'"):
        shortlist.validate_columns(COLUMNS + [{"field": "rent", "label": "Price"}])


def test_norm_address_matches_scraper_rule():
    assert shortlist.norm_address("123 N Main St Apt 4B, Chicago, IL") == "123nmainst#4b"
    assert shortlist.norm_address("123 N. Main St. Suite 4B") == "123nmainst#4b"
    assert shortlist.norm_address(None) == ""


# --------------------------------------------------------------------- rows

def test_build_rows_skips_rejected_and_skipped():
    units = {"a": _unit(unit="1"), "b": _unit(unit="2"), "c": _unit(unit="3"), "d": _unit(unit="4", live=False)}
    actions = {"a": {"status": "Rejected"}, "b": {"status": "Skipped"}, "c": {"status": "Contacted"}}
    rows = shortlist.build_rows(units, {}, {}, actions, COLUMNS)
    assert [r["unit_id"] for r in rows] == ["c"]


def test_build_rows_private_name():
    owner = _unit(building=None, advertiser_type={"value": "owner", "status": "FACT", "source": "https://x.test"})
    rows = shortlist.build_rows({"o": owner, "n": _unit(building=None, unit="9")}, {}, {}, {}, COLUMNS)
    names = {r["unit_id"]: r["name"] for r in rows}
    assert names == {"o": "Private", "n": ""}


def test_build_rows_user_columns_blank():
    buildings = {"tower-2": {"walk": {"score": 92}}}
    (row,) = shortlist.build_rows({"t": _unit()}, buildings, {}, {}, COLUMNS)
    assert row["review"] == ""
    assert row["walkScore"] == 92 and row["price"] == 3400 and row["availability"] == "2026-11-01"
    assert row["address"] == "1 Example Ave Unit 2207, Chicago, IL"
    assert row["key"] == "1exampleave#2207" and row["unit_id"] == "t"


def test_build_rows_link_fallback_order():
    full = _unit()
    no_deep = _unit(unit="2", unit_deep_link=None)
    only_url = _unit(unit="3", unit_deep_link=None, verify_url=None)
    rows = {r["unit_id"]: r["link"] for r in shortlist.build_rows(
        {"a": full, "b": no_deep, "c": only_url}, {}, {}, {}, COLUMNS)}
    assert rows == {"a": "https://op.test/unit/2207", "b": "https://op.test/verify", "c": "https://op.test/listing"}


# --------------------------------------------------------------------- plan

NOW = "2026-10-03T12:00:00+00:00"


def test_plan_sync_runs_on_new_rows():
    plan = shortlist.plan_sync([{"key": "x", "row_index": 2, "link": "l"}], [_row()], "2026-10-02T12:00:00+00:00", NOW, 7)
    assert plan["run"] is True and len(plan["to_append"]) == 1
    assert plan["to_append"][0]["num"] == 2  # numbered after the document's one row


def test_plan_sync_runs_on_first_sync():
    plan = shortlist.plan_sync([], [], None, NOW, 7)
    assert plan["run"] is True and plan["reason"] == "first sync"


def test_plan_sync_runs_after_min_days():
    doc = [{"key": _row()["key"], "row_index": 2, "link": _row()["link"]}]
    plan = shortlist.plan_sync(doc, [_row()], "2026-09-25T12:00:00+00:00", NOW, 7)
    assert plan["run"] is True and plan["to_append"] == []


def test_plan_sync_skips_when_recent_and_nothing_new():
    doc = [{"key": _row()["key"], "row_index": 2, "link": _row()["link"]}]
    plan = shortlist.plan_sync(doc, [_row()], "2026-10-01T12:00:00+00:00", NOW, 7)
    assert plan["run"] is False and plan["to_append"] == [] and plan["to_refresh"] == []


def test_plan_sync_refreshes_changed_links_only():
    same = _row()
    moved = _row(address="9 Side St, Chicago, IL", link="https://op.test/unit/new")
    doc = [{"key": same["key"], "row_index": 2, "link": same["link"]},
           {"key": moved["key"], "row_index": 3, "link": "https://op.test/unit/old"}]
    plan = shortlist.plan_sync(doc, [same, moved], None, NOW, 7)
    assert plan["to_refresh"] == [{"row_index": 3, "key": moved["key"], "link": "https://op.test/unit/new"}]


# ---------------------------------------------------------------- link gate

def test_link_gate_pass():
    assert shortlist.link_gate(_row(), _evidence())["verdict"] == "PASS"


def test_link_gate_dead_redirect_to_search():
    g = shortlist.link_gate(_row(), _evidence(final_url="https://op.test/search?city=chicago"))
    assert (g["verdict"], g["label"]) == ("FAIL", "dead")
    g = shortlist.link_gate(_row(), _evidence(status=404))
    assert (g["verdict"], g["label"]) == ("FAIL", "dead")


def test_link_gate_dead_no_availability():
    g = shortlist.link_gate(_row(), _evidence(availability_signal=False))
    assert (g["verdict"], g["label"]) == ("FAIL", "dead")


def test_link_gate_non_direct_building_page():
    g = shortlist.link_gate(_row(), _evidence(unit_level=False))
    assert (g["verdict"], g["label"]) == ("FAIL", "non_direct")


def test_link_gate_non_direct_shared_room():
    g = shortlist.link_gate(_row(), _evidence(whole_unit=False))
    assert (g["verdict"], g["label"]) == ("FAIL", "non_direct")


def test_link_gate_flag_abbreviated_street():
    row = _row(address="1200 Washington Blvd Unit 5, Chicago, IL")
    g = shortlist.link_gate(row, _evidence(final_url="https://op.test/u/5",
                                           page_text_excerpt="Unit 5, 1200 W Wash Blvd, available now"))
    assert g["verdict"] == "FLAG"
    wrong = shortlist.link_gate(row, _evidence(final_url="https://op.test/u/5",
                                               page_text_excerpt="Unit 5, 1300 Washington Blvd"))
    assert (wrong["verdict"], wrong["label"]) == ("FAIL", "non_direct")


# -------------------------------------------------------------- summaries

def test_gate_summary_ready_only_when_clean():
    clean = [{"verdict": "PASS"}, {"verdict": "FLAG"}, {"verdict": None, "held": True},
             {"verdict": "FAIL", "label": "dead", "held": True}]
    s = shortlist.gate_summary(clean)
    assert s == {"pass": 1, "flag": 1, "fail_dead": 0, "fail_non_direct": 0, "held": 2, "ready": True}
    assert not shortlist.gate_summary(clean + [{"verdict": "FAIL", "label": "non_direct"}])["ready"]
    assert not shortlist.gate_summary([{"verdict": None}])["ready"]  # no link and not held


def test_recovery_budget_stops_at_three():
    state = {}
    assert [shortlist.recovery_budget(state, "k") for _ in range(4)] == [True, True, True, False]
    assert state["recovery"]["k"] == 4 and shortlist.recovery_budget(state, "other") is True


def test_readback_diff_ignores_user_columns():
    expected = [dict(_row(), num=3, price=3400, review="")]
    observed = [dict(_row(), num="3", price="3400", review="loved the light")]
    assert shortlist.readback_diff(expected, observed) == []
    observed[0]["price"] = "3500"
    assert shortlist.readback_diff(expected, observed) == [
        {"key": expected[0]["key"], "field": "price", "expected": "3400", "observed": "3500"}]
    assert shortlist.readback_diff(expected, [])[0]["field"] is None
