"""Tests for feed_refresh.py - feed diffs never prove removal on their own."""

from __future__ import annotations

import feed_refresh as fr


def _units():
    return [
        {"token": "a", "rent_verified": 3000, "live": True},
        {"token": "b", "rent_verified": 3100, "live": True},
        {"token": "c", "rent_verified": 3580, "live": True},
        {"token": "d", "rent_verified": 2900, "live": False, "availability_state": "delisted",
         "delisted": "2026-09-01"},
    ]


def test_complete_feed_marks_possibly_missing_never_delisted():
    out = fr.diff_feed({"a": 2950, "x": 2000}, _units(), feed_complete=True, price_ceiling=3600)
    assert out["price_changed"] == [{"token": "a", "old": 3000.0, "new": 2950.0,
                                     "why": out["price_changed"][0]["why"]}]
    states = {m["token"]: m["suggested_state"] for m in out["missing"]}
    assert states == {"b": "possibly_missing", "c": "possibly_missing"}
    assert "near the search ceiling" in next(m for m in out["missing"] if m["token"] == "c")["why"]
    assert out["new_tokens"] == ["x"]
    assert any("ceiling" in c for c in out["caveats"])


def test_partial_feed_yields_unknown():
    out = fr.diff_feed({"a": 3000}, _units(), feed_complete=False)
    assert {m["suggested_state"] for m in out["missing"]} == {"unknown"}
    assert out["unchanged"] == ["a"]


def test_delisted_reappearance_needs_confirmation():
    out = fr.diff_feed({"a": 3000, "b": 3100, "c": 3580, "d": 2900}, _units(), feed_complete=True)
    assert [r["token"] for r in out["reappeared"]] == ["d"]


def test_verification_plan_all_then_sample():
    small = fr.verification_plan([str(i) for i in range(15)])
    assert small["mode"] == "all" and len(small["check"]) == 15
    big = fr.verification_plan([str(i) for i in range(40)], seed=1)
    assert big["mode"] == "sample"
    assert len(big["check"]) == 8 and len(big["unsampled"]) == 32
    assert "8 of 40" in big["note"]


def test_apply_verdicts_rules():
    units = _units()
    summary = fr.apply_verdicts(units, {
        "a": {"verdict": "live", "price": 2950, "price_trust": "ok", "verified_at": "2026-10-03T10:00:00+00:00"},
        "b": {"verdict": "gone"},
        "c": {"verdict": "check"},
        "d": {"verdict": "live", "price": None},
    }, "2026-10-03")
    by = {u["token"]: u for u in units}
    assert by["a"]["availability_state"] == "active" and by["a"]["price_checked"] == "2026-10-03"
    assert by["b"]["availability_state"] == "delisted" and by["b"]["live"] is False
    assert "availability_state" not in by["c"]
    assert by["d"]["availability_state"] == "active" and "delisted" not in by["d"]
    assert "price_checked" not in by["d"]
    assert summary["unchanged"] == ["c"]


def test_mark_missing_skips_checked():
    units = _units()
    changed = fr.mark_missing(units, [{"token": "b", "suggested_state": "possibly_missing"},
                                      {"token": "c", "suggested_state": "possibly_missing"}], {"c"})
    assert changed == ["b"]
    assert units[1]["availability_state"] == "possibly_missing"
