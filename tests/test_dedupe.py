"""Tests for dedupe.py - same-unit matching and leverage signals."""

from __future__ import annotations

import dedupe


def _u(uid, **kw):
    base = {"unit_id": uid, "lat": 41.88001, "lon": -87.63001, "floor": 12, "beds": 2, "live": True}
    base.update(kw)
    return base


def test_match_ignores_address_string_uses_coords_floor_beds():
    a = _u("a", address="34 Main St", unit="12B")
    b = _u("b", address="36 Main St", unit="12b", lat=41.880012)
    assert dedupe.match(a, b) == "strong"


def test_coords_alone_are_only_a_candidate():
    assert dedupe.match(_u("a"), _u("b")) == "candidate"
    assert dedupe.match(_u("a", unit="12A"), _u("b", unit="12C")) is None
    assert dedupe.match(_u("a"), _u("b", floor=13)) is None
    assert dedupe.match(_u("a"), {"unit_id": "b"}) is None


def test_token_match():
    a = {"token": "x1", "source": "site"}
    assert dedupe.match(a, {"token": "x1", "source": "site"}) == "token"


def test_find_matches_includes_gone_units():
    gone = _u("old", unit="12B", live=False)
    out = dedupe.find_matches([_u("new", unit="12B")], [gone])
    assert out[0]["level"] == "strong" and out[0]["tracked_gone"] and out[0]["auto_merge"]


def test_signals_owner_vs_agency_and_price_gap():
    g = [
        _u("o", unit="12B", advertiser_type="owner", rent_verified=3000),
        _u("a1", unit="12B", advertiser_type="agency", agency="Acme", rent_verified=3100),
        _u("a2", unit="12B", advertiser_type="agency", agency="acme", rent_verified=3250),
    ]
    sig = {s["signal"]: s for s in dedupe.classify_group(g)}
    assert sig["owner_and_agency"]["prefer"] == "o"
    assert sig["same_agent_price_gap"]["prices"] == [3100.0, 3250.0]
    assert dedupe.choose_primary(g)["primary"] == "o"


def test_relist_signals_and_size_growth():
    g = [
        _u("v1", unit="4", rent_verified=3000, sqft=900, published_at="2026-08-01T09:00:00+00:00", live=False),
        _u("v2", unit="4", rent_verified=3000, sqft=980, published_at="2026-09-01T09:00:00+00:00"),
        _u("v3", unit="4", rent_verified=2850, sqft=980, published_at="2026-09-20T09:00:00+00:00"),
    ]
    names = [s["signal"] for s in dedupe.classify_group(g)]
    assert "relisted_same_price" in names
    assert "relisted_cheaper" in names
    assert "size_growth" in names


def test_feed_flooding():
    g = [
        _u("f1", unit="7", agency="Z", sqft=800, published_at="2026-09-01T09:00:00+00:00"),
        _u("f2", unit="7", agency="Z", sqft=870, published_at="2026-09-01T09:12:00+00:00"),
    ]
    assert "feed_flooding" in [s["signal"] for s in dedupe.classify_group(g)]


def test_group_units_skips_candidates():
    units = [_u("a"), _u("b"), _u("c", unit="1"), _u("d", unit="1")]
    groups = dedupe.group_units(units)
    assert [sorted(u["unit_id"] for u in g) for g in groups] == [["c", "d"]]


def test_primary_prefers_live_then_cheapest():
    g = [_u("gone", unit="1", rent_verified=1000, live=False),
         _u("p1", unit="1", rent_verified=3000), _u("p2", unit="1", rent_verified=2900)]
    res = dedupe.choose_primary(g)
    assert res["primary"] == "p2"
    assert {a["unit_id"] for a in res["alternative_sources"]} == {"gone", "p1"}
