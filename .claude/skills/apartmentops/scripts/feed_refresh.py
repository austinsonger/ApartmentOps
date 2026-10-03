#!/usr/bin/env python3
"""Feed-first price refresh and honest removal tracking.

A search feed usually carries token -> price for every result, so
refreshing the whole tracked set costs the same handful of page requests
as discovery, not one request per unit. This module diffs a full feed
read against the tracked units and decides which units need an individual
re-check. It never marks anything gone on its own.

The rules it encodes:

- Feed absence alone never proves removal. Filters, a price rise above
  the search ceiling, an incomplete scan, or an access failure all hide
  live ads. A unit missing from the feed becomes `possibly_missing`.
- Verify EVERY missing unit individually when there are few of them
  (default <= 15); above that, verify a sample of at least 8 and report
  the ratio. Sampling never justifies marking an unsampled unit gone.
- Only a confirmed individual check (verify_units.py verdict `gone`)
  makes a unit `delisted`, with the date; its history stays.
- A verified reappearance clears `delisted`; a `check` verdict changes
  nothing.
- `price_checked` is stamped only on units whose price was actually read
  this run.

Availability values (stored as `availability_state` on a verified.json
unit, alongside the existing `live` boolean):
    active | possibly_missing | unknown | delisted

Stdlib only, no network.

CLI:
    python3 feed_refresh.py diff feed.json verified.json [--complete] [--ceiling N]
"""

from __future__ import annotations

import json
import random
import sys
from typing import Any

FULL_VERIFY_THRESHOLD = 15
MIN_SAMPLE = 8
NEAR_CEILING_PCT = 5.0
STATES = ("active", "possibly_missing", "unknown", "delisted")


def _token(unit: dict) -> str | None:
    tok = unit.get("token") or unit.get("unit_token")
    return str(tok) if tok not in (None, "") else None


def _price(unit: dict) -> float | None:
    for key in ("rent_verified", "price", "rent_gross"):
        val = unit.get(key)
        if isinstance(val, dict):
            val = val.get("value")
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val)
    return None


def diff_feed(
    feed: dict[str, Any],
    units: list[dict],
    *,
    feed_complete: bool,
    price_ceiling: float | None = None,
) -> dict:
    """Compare a full feed read (token -> price or null) with tracked units.

    feed_complete must be True only when checked.coverage() said the scan
    read every page with no blocker. On a partial feed, missing units are
    `unknown` rather than `possibly_missing` - nothing can be concluded.

    Returns {price_changed, missing, reappeared, unchanged, new_tokens,
    caveats}. Each missing entry carries `suggested_state` and `why`.
    """
    feed = {str(k): v for k, v in feed.items()}
    tracked: dict[str, dict] = {}
    for u in units:
        tok = _token(u)
        if tok:
            tracked[tok] = u

    out: dict[str, Any] = {
        "price_changed": [],
        "missing": [],
        "reappeared": [],
        "unchanged": [],
        "new_tokens": sorted(t for t in feed if t not in tracked),
        "caveats": [],
    }
    for tok, unit in sorted(tracked.items()):
        delisted = unit.get("availability_state") == "delisted" or unit.get("delisted")
        if tok in feed:
            if delisted:
                out["reappeared"].append({
                    "token": tok,
                    "why": "in feed again; confirm with an individual check before clearing delisted",
                })
                continue
            new_price = feed[tok]
            old_price = _price(unit)
            if isinstance(new_price, (int, float)) and old_price is not None and float(new_price) != old_price:
                out["price_changed"].append({
                    "token": tok,
                    "old": old_price,
                    "new": float(new_price),
                    "why": "feed price differs; confirm on the unit's own page before writing (feed price is not an admissible price layer by itself)",
                })
            else:
                out["unchanged"].append(tok)
            continue
        if delisted:
            continue
        why = []
        if not feed_complete:
            state = "unknown"
            why.append("feed read was partial; absence proves nothing")
        else:
            state = "possibly_missing"
            why.append("absent from a complete feed read")
        old_price = _price(unit)
        if price_ceiling and old_price is not None and old_price >= price_ceiling * (1 - NEAR_CEILING_PCT / 100):
            why.append("last price was near the search ceiling; a price rise would drop it from the feed")
        out["missing"].append({"token": tok, "suggested_state": state, "why": "; ".join(why)})

    if price_ceiling:
        out["caveats"].append(
            "the feed is filtered by the price ceiling: a unit whose price rose above it disappears and looks removed"
        )
    if not feed_complete:
        out["caveats"].append("partial feed: no unit may be marked possibly_missing or delisted from this read")
    return out


def verification_plan(
    missing_tokens: list[str],
    *,
    full_threshold: int = FULL_VERIFY_THRESHOLD,
    min_sample: int = MIN_SAMPLE,
    seed: int | None = None,
) -> dict:
    """Which missing units to re-check individually.

    <= full_threshold: all of them. Above it: a random sample of at least
    min_sample, and the report must state the ratio. Unsampled units stay
    possibly_missing - never extrapolated to gone.
    """
    tokens = sorted(set(map(str, missing_tokens)))
    if len(tokens) <= full_threshold:
        return {"mode": "all", "check": tokens, "unsampled": [], "note": f"verifying all {len(tokens)} missing"}
    rng = random.Random(seed)
    sample = sorted(rng.sample(tokens, min_sample))
    rest = [t for t in tokens if t not in sample]
    return {
        "mode": "sample",
        "check": sample,
        "unsampled": rest,
        "note": f"sampled {len(sample)} of {len(tokens)} missing; the other {len(rest)} stay possibly_missing",
    }


def apply_verdicts(units: list[dict], verdicts: dict[str, dict], today: str) -> dict:
    """Apply individual re-check verdicts (verify_units.py rows keyed by
    token: {"verdict": live|gone|check, "price": n|None, "price_trust": ..}).

    Mutates units in place and returns a summary. Rules:
      live  -> availability_state active, delisted cleared, price_checked
               stamped only when an admissible price was read
      gone  -> availability_state delisted, delisted = today, live False
      check -> nothing changes (a check never overturns anything)
    """
    summary = {"active": [], "delisted": [], "unchanged": []}
    by_token = {_token(u): u for u in units if _token(u)}
    for tok, row in sorted(verdicts.items()):
        unit = by_token.get(str(tok))
        if unit is None:
            continue
        verdict = row.get("verdict")
        if verdict == "live":
            unit["availability_state"] = "active"
            unit.pop("delisted", None)
            unit["live"] = True
            unit["verified_at"] = row.get("verified_at", unit.get("verified_at"))
            if row.get("price") is not None and row.get("price_trust", "ok") == "ok":
                unit["price_checked"] = today
            summary["active"].append(tok)
        elif verdict == "gone":
            unit["availability_state"] = "delisted"
            unit["delisted"] = today
            unit["live"] = False
            summary["delisted"].append(tok)
        else:
            summary["unchanged"].append(tok)
    return summary


def mark_missing(units: list[dict], missing: list[dict], skip_tokens: set[str]) -> list[str]:
    """Stamp suggested_state onto missing units that were NOT individually
    checked this run (so the dashboard can show 'possibly missing')."""
    changed = []
    by_token = {_token(u): u for u in units if _token(u)}
    for row in missing:
        tok = row["token"]
        if tok in skip_tokens or tok not in by_token:
            continue
        by_token[tok]["availability_state"] = row["suggested_state"]
        changed.append(tok)
    return changed


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) >= 3 and args[0] == "diff":
        feed = json.load(open(args[1], encoding="utf-8"))
        units = json.load(open(args[2], encoding="utf-8"))
        if isinstance(units, dict):
            units = list(units.values())
        ceiling = None
        if "--ceiling" in args:
            ceiling = float(args[args.index("--ceiling") + 1])
        result = diff_feed(feed, units, feed_complete="--complete" in args, price_ceiling=ceiling)
        result["plan"] = verification_plan([m["token"] for m in result["missing"]])
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
