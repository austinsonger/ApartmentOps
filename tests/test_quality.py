"""Tests for quality.py - traps, fees, freshness, note hygiene, integrity."""

from __future__ import annotations

import quality

CFG = {"budget": {"gross_min": 2500, "gross_max": 3500}, "unit": {"beds": 2}}
NOW = "2026-10-03T12:00:00+00:00"


def _flags(unit, **kw):
    return {f["flag"]: f for f in quality.traps(unit, CFG, **kw)}


def test_price_unstated_skips():
    assert quality.traps({"rent_verified": 0}, CFG)[0]["action"] == "skip"
    assert quality.traps({}, CFG)[0]["flag"] == "price_unstated"


def test_shared_and_sublet_skip():
    assert _flags({"rent_verified": 3000, "property_type": "Sublet - 3 months"})["shared_or_sublet"]["action"] == "skip"
    assert "shared_or_sublet" in _flags({"rent_verified": 3000, "property_type": "Room in a shared apartment"})
    cfg = {"unit": {"whole_unit_only": False}}
    assert not quality.traps({"rent_verified": 3000, "property_type": "private room"}, cfg)


def test_below_floor_and_comps():
    comps = [{"beds": 2, "rent_verified": p} for p in (3000, 3100, 3200)]
    f = _flags({"beds": 2, "rent_verified": 1000}, comps=comps)
    assert f["below_floor"]["action"] == "no_star"
    assert f["far_below_comps"]["comps_n"] == 3


def test_implausible_size_sqm_and_sqft():
    assert "implausible_size" in _flags({"beds": 2, "rent_verified": 3000, "sqft": 2400})
    assert "implausible_size" in _flags({"beds": 2, "rent_verified": 3000, "size_sqm": 220})
    assert "implausible_size" not in _flags({"beds": 2, "rent_verified": 3000, "sqft": 1050})


def test_placeholder_fee_and_normalize():
    f = _flags({"rent_verified": 3000, "fees": [{"type": "hoa", "amount": 9999, "billing_period": "monthly"}]})
    assert f["placeholder_fee"]["action"] == "warn"
    n = quality.normalize_fee({"type": "hoa", "amount": 9999, "billing_period": "monthly"})
    assert n["monthly"] is None and n["status"] == "MISSING"
    assert quality.normalize_fee({"amount": 600, "billing_period": "bimonthly"})["monthly"] == 300
    assert quality.normalize_fee({"amount": 600})["monthly"] is None
    assert quality.normalize_fee({"amount": 600, "billing_period": "one_time"})["monthly"] is None


def test_known_monthly_total_partial():
    t = quality.known_monthly_total({"rent_verified": 3000, "fees": [
        {"type": "amenity", "amount": 50, "billing_period": "monthly"},
        {"type": "property_tax", "amount": 400, "billing_period": "bimonthly"},
        {"type": "committee", "amount": 9999, "billing_period": "monthly"},
        {"type": "application", "amount": 75, "billing_period": "one_time"},
    ]})
    assert t["total"] == 3250 and t["partial"] and t["missing"] == ["committee"]


def test_over_budget_all_in():
    f = _flags({"rent_verified": 3400, "fees": [{"type": "amenity", "amount": 200, "billing_period": "monthly"}]})
    assert f["over_budget_all_in"]["total"] == 3600


def test_freshness_never_invents_timezone():
    assert quality.freshness({"published_at": "2026-10-03T09:00:00"}, NOW)["hours"] is None
    f = quality.freshness({"published_at": "2026-10-03T09:00:00"}, NOW, source_tz="Asia/Jerusalem")
    assert f["hours"] == 6.0  # 09:00 IDT (UTC+3) -> 06:00Z, six hours before NOW
    assert quality.freshness({"published_at": "2026-10-03T10:00:00+00:00"}, NOW)["hours"] == 2.0


def test_stale_and_bumped():
    u = {"rent_verified": 3000, "published_at": "2025-06-01T09:00:00+00:00",
         "updated_at": "2026-10-01T09:00:00+00:00"}
    f = _flags(u, now=NOW)
    assert f["stale_publication"]["action"] == "no_star"
    assert f["bumped_listing"]["action"] == "info"


def test_odd_price_info():
    assert "odd_price" in _flags({"rent_verified": 3013})
    assert "odd_price" not in _flags({"rent_verified": 3015})


def test_worst_action():
    assert quality.worst_action([{"action": "info"}, {"action": "no_star"}]) == "no_star"
    assert quality.worst_action([]) is None


def test_relative_time_sweep():
    hits = quality.relative_time_hits([
        {"unit_id": "a", "notes": "Posted today, hurry!"},
        {"unit_id": "b", "notes": "Published 2026-10-01; 3 days ago"},
        {"unit_id": "c", "notes": "Listed 2026-09-14 at 3,100."},
    ])
    assert [h["unit_id"] for h in hits] == ["a", "b"]


def test_integrity_report(tmp_path):
    units = [
        {"unit_id": "x", "rent_verified": 3000, "beds": 2, "screenshot": "shots/x.png", "live": True},
        {"unit_id": "x", "rent_verified": 4000, "beds": 1, "live": True},
        {"unit_id": "y", "rent_verified": 3000, "beds": 2, "screenshot": "https://cdn/y.png",
         "lat": 1, "lon": 1, "floor": 2},
        {"unit_id": "z", "rent_verified": 3000, "beds": 2, "lat": 1, "lon": 1, "floor": 2},
    ]
    r = quality.integrity_report(units, CFG, root=str(tmp_path))
    assert r["duplicate_ids"] == ["x"]
    assert r["missing_files"] == [{"unit_id": "x", "field": "screenshot", "path": "shots/x.png"}]
    assert r["out_of_spec"] == [{"unit_id": "x", "why": ["over budget", "too few beds"]}]
    assert r["same_unit_pairs"] == [{"a": "y", "b": "z", "level": "candidate"}]
    assert not r["ok"]


def test_shared_patterns_new_phrases():
    for phrase in ("Room for rent", "Shared room near transit", "Looking for a housemate",
                   "House share, 3 people", "Room in house", "Room in apartment"):
        f = _flags({"rent_verified": 3000, "property_type": phrase})
        assert f["shared_or_sublet"]["action"] == "skip", phrase


def test_shared_patterns_false_positives():
    for phrase in ("Laundry room in basement", "Bedroom with closet", "Big living room", "Sunroom"):
        assert "shared_or_sublet" not in _flags({"rent_verified": 3000, "property_type": phrase}), phrase


def test_net_without_gross_flag():
    u = {"rent_verified": 3900, "rent_is_net": True, "notes": "net $3,900/mo with 1 month free"}
    assert _flags(u)["net_without_gross"]["action"] == "warn"
    u["notes"] = "net $3,900/mo / gross $4,200"
    assert "net_without_gross" not in _flags(u)


def test_net_without_gross_hits_sweep():
    hits = quality.net_without_gross_hits([
        {"unit_id": "a", "notes": "Net-effective $3,900 on a 13-month term"},
        {"unit_id": "b", "notes": "gross $4,200 / net $3,900"},
    ])
    assert len(hits) == 1 and hits[0]["unit_id"] == "a"
    assert "Net-effective" in hits[0]["note_excerpt"]


BUDGET = {"gross_max": 3500, "gross_max_stretch": 3800}


def test_concession_badge_matches():
    for text in ("1 month free", "6 weeks free", "2 mo free", "Move-in special", "move in special",
                 "Special offer", "Special!", "Starting at $3,400", "starting from 3,400", "From $3,400",
                 "Concession on 13-month lease", "Look and lease", "Limited-time deal", "limited time"):
        assert quality.concession_badge(text), text
    for text in ("Ask the specialist", "Near special education center", None, ""):
        assert not quality.concession_badge(text), text


def test_feed_price_decision_pass_under_ceiling():
    assert quality.feed_price_decision(3400, "", BUDGET) == {"action": "pass"}


def test_feed_price_decision_open_badged_within_stretch():
    d = quality.feed_price_decision(3700, "6 weeks free", BUDGET)
    assert d == {"action": "open", "reason": "concession_badged_over_ceiling"}
    # opened on the exception, verified over the ceiling: the existing warn fires
    opened = {"rent_verified": 3650, "quality_flags": ["concession_badged_over_ceiling"]}
    assert _flags(opened)["over_budget_all_in"]["action"] == "warn"
    assert "over_budget_all_in" not in _flags({**opened, "rent_verified": 3400})


def test_feed_price_decision_skip_badged_above_stretch():
    d = quality.feed_price_decision(3900, "6 weeks free", BUDGET)
    assert d == {"action": "skip", "reason": "over_ceiling"}
    # no stretch set: the stretch ceiling is gross_max, so a badge cannot open it
    assert quality.feed_price_decision(3600, "Special", {"gross_max": 3500})["action"] == "skip"


def test_feed_price_decision_skip_unbadged_over_ceiling():
    assert quality.feed_price_decision(3700, "Bright corner unit", BUDGET) == {"action": "skip", "reason": "over_ceiling"}


def test_feed_price_decision_none_price_opens():
    assert quality.feed_price_decision(None, "", BUDGET) == {"action": "open", "reason": "price_unstated_on_card"}
