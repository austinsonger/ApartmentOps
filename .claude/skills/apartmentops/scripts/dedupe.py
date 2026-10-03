#!/usr/bin/env python3
"""Same-unit detection across listings and the leverage it reveals.

Duplicates are leverage, not noise. The same unit advertised twice tells
the user something: two prices from one agent, a relist that never rented,
a real price cut, or an owner ad that saves a broker fee.

Matching keys on coordinates + floor + bedrooms (or rooms), NOT on the
address string: hand-typed house numbers are often blank or off by one or
two, so one building shows up as "34 Main" and "36 Main". Every candidate
is compared against the WHOLE tracked set, gone units included.

Match levels (strongest first):
    token     - identical source token (same ad)
    strong    - same coords cell + floor + beds + identical unit number
    candidate - same coords cell + floor + beds, no unit-number
                corroboration. Rounded building coordinates alone do NOT
                prove a match; candidates are preserved for human review,
                never auto-merged.

Leverage signals (classify_group):
    same_agent_price_gap  - one advertiser, two live ads, different prices
    relisted_same_price   - reappeared >= 14 days after first seen, same price
    relisted_cheaper      - reappeared at a lower price (a real cut)
    owner_and_agency      - the same unit from the owner and from a broker:
                            prefer the owner ad (no broker fee)
    size_growth           - declared size grows across reposts: distrust
                            that advertiser's numbers, not a bigger unit
    feed_flooding         - one advertiser, several ads minutes apart for
                            the same unit with disagreeing sizes
Negotiation readings are inferences, never proof of owner pressure.

Stdlib only, no network.

CLI:
    python3 dedupe.py verified.json [new.json]
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from typing import Any

COORD_DECIMALS = 4  # ~11 m: one building, not one block
RELIST_GAP_DAYS = 14
FLOOD_WINDOW_MINUTES = 60
NO_FEE_TYPES = {"owner", "private", "no_fee", "building_direct"}


def _val(unit: dict, key: str):
    v = unit.get(key)
    if isinstance(v, dict) and "value" in v:
        return v.get("value")
    return v


def _num(unit: dict, *keys: str) -> float | None:
    for k in keys:
        v = _val(unit, k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _ts(value) -> _dt.datetime | None:
    if not value:
        return None
    try:
        t = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else None


def _first_seen(unit: dict) -> _dt.datetime | None:
    for k in ("published_at", "found_at", "verified_at"):
        t = _ts(unit.get(k))
        if t:
            return t
    return None


def unit_cell(unit: dict) -> tuple | None:
    """(lat, lon, floor, beds) cell or None when any part is unknown."""
    lat = _num(unit, "lat")
    lon = _num(unit, "lon", "lng")
    floor = _val(unit, "floor")
    beds = _num(unit, "beds", "rooms")
    if lat is None or lon is None or floor is None or beds is None:
        return None
    return (round(lat, COORD_DECIMALS), round(lon, COORD_DECIMALS), str(floor), beds)


def _uid(unit: dict) -> str:
    return str(unit.get("unit_id") or unit.get("id") or unit.get("token") or unit.get("url"))


def match(a: dict, b: dict) -> str | None:
    """Match level between two listings, or None."""
    ta, tb = a.get("token"), b.get("token")
    if ta and tb and str(ta) == str(tb) and a.get("source") == b.get("source"):
        return "token"
    ca, cb = unit_cell(a), unit_cell(b)
    if ca is None or ca != cb:
        return None
    ua, ub = _val(a, "unit"), _val(b, "unit")
    if ua and ub and str(ua).strip().lower() == str(ub).strip().lower():
        return "strong"
    if ua and ub:
        return None  # same floor, different unit numbers: neighbours, not twins
    return "candidate"


def find_matches(new_units: list[dict], tracked: list[dict]) -> list[dict]:
    """Every (new, tracked) pair that matches, tracked including gone units."""
    out = []
    for n in new_units:
        for t in tracked:
            if n is t:
                continue
            level = match(n, t)
            if level:
                out.append({
                    "new": _uid(n),
                    "tracked": _uid(t),
                    "level": level,
                    "auto_merge": level in ("token", "strong"),
                    "tracked_gone": t.get("live") is False or t.get("availability_state") == "delisted",
                })
    return out


def group_units(units: list[dict]) -> list[list[dict]]:
    """Cluster units into same-unit groups (token or strong matches only)."""
    parent = list(range(len(units)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(units)):
        for j in range(i + 1, len(units)):
            if match(units[i], units[j]) in ("token", "strong"):
                parent[find(i)] = find(j)
    groups: dict[int, list[dict]] = {}
    for i, u in enumerate(units):
        groups.setdefault(find(i), []).append(u)
    return [g for g in groups.values() if len(g) > 1]


def _is_no_fee(unit: dict) -> bool:
    adv = str(unit.get("advertiser_type") or "").lower()
    return adv in NO_FEE_TYPES or unit.get("broker_fee") == 0


def classify_group(group: list[dict]) -> list[dict]:
    """Leverage signals for one same-unit group. Each signal carries the
    evidence (unit ids, prices, dates) so the note can cite it."""
    signals: list[dict] = []
    live = [u for u in group if u.get("live") is not False and u.get("availability_state") != "delisted"]

    by_agent: dict[str, list[dict]] = {}
    for u in live:
        agent = str(u.get("agency") or u.get("advertiser") or "").strip().lower()
        if agent:
            by_agent.setdefault(agent, []).append(u)
    for agent, ads in by_agent.items():
        prices = sorted({p for p in (_num(u, "rent_verified", "price", "rent_gross") for u in ads) if p is not None})
        if len(prices) > 1:
            cheapest = min(ads, key=lambda u: _num(u, "rent_verified", "price", "rent_gross") or 1e18)
            signals.append({
                "signal": "same_agent_price_gap",
                "units": [_uid(u) for u in ads],
                "prices": prices,
                "cheapest_url": cheapest.get("url"),
                "meaning": "same advertiser lists this unit at different prices; quote the cheaper ad",
            })
        stamps = sorted((t, u) for u in ads if (t := _first_seen(u)))
        sizes = [_num(u, "sqft", "size_sqm") for _, u in stamps]
        if len(stamps) > 1:
            span = (stamps[-1][0] - stamps[0][0]).total_seconds() / 60
            known = [s for s in sizes if s is not None]
            if span <= FLOOD_WINDOW_MINUTES and len(set(known)) > 1:
                signals.append({
                    "signal": "feed_flooding",
                    "units": [_uid(u) for _, u in stamps],
                    "sizes": known,
                    "meaning": "one advertiser posted the same unit repeatedly with disagreeing sizes; keep the cheapest and distrust its stated sizes",
                })

    ordered = sorted(((t, u) for u in group if (t := _first_seen(u))), key=lambda x: x[0])
    for (t0, u0), (t1, u1) in zip(ordered, ordered[1:]):
        gap = (t1 - t0).days
        p0 = _num(u0, "rent_verified", "price", "rent_gross")
        p1 = _num(u1, "rent_verified", "price", "rent_gross")
        if gap >= RELIST_GAP_DAYS and p0 is not None and p1 is not None:
            if p1 == p0:
                signals.append({
                    "signal": "relisted_same_price",
                    "units": [_uid(u0), _uid(u1)],
                    "first_seen": t0.isoformat(),
                    "days_circulating": gap,
                    "meaning": f"relisted at the same price after {gap} days; it did not rent (inference: room to negotiate)",
                })
            elif p1 < p0:
                signals.append({
                    "signal": "relisted_cheaper",
                    "units": [_uid(u0), _uid(u1)],
                    "from": p0,
                    "to": p1,
                    "meaning": f"relisted cheaper ({p0:g} -> {p1:g}); a real price cut",
                })
        s0, s1 = _num(u0, "sqft", "size_sqm"), _num(u1, "sqft", "size_sqm")
        if s0 is not None and s1 is not None and s1 > s0:
            signals.append({
                "signal": "size_growth",
                "units": [_uid(u0), _uid(u1)],
                "from": s0,
                "to": s1,
                "meaning": "declared size grew across reposts; a red flag on the advertiser's numbers, not a bigger unit",
            })

    owners = [u for u in live if _is_no_fee(u)]
    agents = [u for u in live if not _is_no_fee(u) and u.get("advertiser_type")]
    if owners and agents:
        signals.append({
            "signal": "owner_and_agency",
            "units": [_uid(u) for u in owners + agents],
            "prefer": _uid(owners[0]),
            "meaning": "same unit offered directly and through a broker; keep the direct ad to avoid a broker fee",
        })
    return signals


def choose_primary(group: list[dict]) -> dict:
    """Which record represents the group: live over gone, no-fee over
    broker, then cheapest, then earliest seen. The rest become
    alternative_sources on the primary (never discarded)."""
    def key(u):
        live = u.get("live") is not False and u.get("availability_state") != "delisted"
        price = _num(u, "rent_verified", "price", "rent_gross")
        seen = _first_seen(u)
        return (
            0 if live else 1,
            0 if _is_no_fee(u) else 1,
            price if price is not None else float("inf"),
            seen.timestamp() if seen else float("inf"),
        )
    primary = min(group, key=key)
    return {
        "primary": _uid(primary),
        "alternative_sources": [
            {"unit_id": _uid(u), "url": u.get("url"), "price": _num(u, "rent_verified", "price", "rent_gross"),
             "advertiser_type": u.get("advertiser_type")}
            for u in group if u is not primary
        ],
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__)
        return 2
    tracked = json.load(open(args[0], encoding="utf-8"))
    if isinstance(tracked, dict):
        tracked = list(tracked.values())
    if len(args) > 1:
        new = json.load(open(args[1], encoding="utf-8"))
        print(json.dumps(find_matches(new, tracked), indent=2, ensure_ascii=False))
        return 0
    report = []
    for g in group_units(tracked):
        report.append({**choose_primary(g), "signals": classify_group(g)})
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
