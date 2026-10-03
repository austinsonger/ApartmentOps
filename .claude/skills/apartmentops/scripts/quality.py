#!/usr/bin/env python3
"""Listing data-quality traps, publication freshness, fee periods, note
hygiene, and the pre-ship integrity check.

Every trap here was seen in the wild. Each returns a flag with an action:

    skip     - do not ingest (record the rejection in checked.json)
    no_star  - ingest, but never TourNow / never a value badge
    warn     - ingest, put the warning in the unit's notes
    info     - worth one line in the notes

Traps:
    price_unstated     rent missing or 0                              skip
    shared_or_sublet   property type says room / shared / sublet,
                       when config wants a whole unit                 skip
    below_floor        rent below the configured floor; often a room
                       in a shared unit, or bait                      no_star
    far_below_comps    < 40% of the median of same-bed comps          no_star
    implausible_size   size far outside the bed count's normal range  no_star
    placeholder_fee    9999-style fee amounts: "not stated"           warn
    stale_publication  originally published more than a year ago      no_star
    bumped_listing     updated long after creation: refreshed to climb
                       the feed, and a sign it is not renting         info
    odd_price          not a round number: owner computed it; a
                       serious ad, less room to haggle                info
    over_budget_all_in rent + known recurring fees above gross_max:
                       must be the FIRST sentence of the notes        warn
    net_without_gross  rent_is_net, and the notes state a net figure
                       with no gross beside it                        warn

Freshness: `published_at` is the original publication timestamp, kept in
full (hour-level freshness matters) with its timezone. A refreshed
`updated_at` is never used as publication time. A naive timestamp is never
given an invented timezone: freshness returns None with a reason unless
the caller passes the source's known timezone.

Stdlib only, no network.

CLI:
    python3 quality.py traps verified.json [config.json]
    python3 quality.py notes verified.json
    python3 quality.py integrity verified.json [config.json]
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
import re
import statistics
import sys
from typing import Any
from zoneinfo import ZoneInfo

STALE_DAYS = 365
BUMP_GAP_DAYS = 7
FAR_BELOW_COMPS = 0.40
PLACEHOLDER_FEE = re.compile(r"^9{3,}$")
SHARED_PATTERNS = re.compile(
    r"\b(sublet|sublease|room in (a |an |the )?(house|apartment|apt|unit|flat)|room for rent|"
    r"private room|shared room|shared|roommate|housemate|house share|rooming)\b",
    re.IGNORECASE,
)
# Relative time goes stale the day after it is written. Notes carry
# absolute dates; the dashboard's live badge says how fresh a listing is.
RELATIVE_TIME = re.compile(
    r"\b(today|tonight|yesterday|tomorrow|just (posted|listed)|hurry|this (week|morning|afternoon)|"
    r"\d+\s*(minutes?|hours?|days?)\s+ago|posted recently|brand new listing)\b",
    re.IGNORECASE,
)
# A net-effective figure written without the gross beside it reads as the
# monthly check. It is not: the gross is what is paid most months and what
# renewal starts from.
NET_WITHOUT_GROSS = re.compile(r"\bnet[- ]effective\b|\bnet\s*\$|\$[\d,]+\s*net\b", re.IGNORECASE)
# A feed card carrying a concession or "starting at" badge hides the true
# unit rent: the card shows a teaser, the page shows the unit. Such a card
# over the ceiling (but within the stretch) is opened, not skipped. A bare
# "special" is excluded before "education" / "needs" so school and service
# copy does not read as a rent badge.
CONCESSION_BADGE = re.compile(
    r"\b(\d+\s*(month|months|mo|weeks?) free|move[- ]in special|special offer|"
    r"special(?!\s+(education|needs)\b)|starting (at|from)|from \$|concession|look and lease|"
    r"limited[- ]time)\b",
    re.IGNORECASE,
)
# Typical size bands per bedroom count (square feet). Outside 0.5x-2x of
# these reads as lot/garden area, a typo, or a wrong bed count.
SQFT_TYPICAL = {0: 450, 1: 700, 2: 1000, 3: 1300, 4: 1700}
SQM_PER_SQFT = 0.092903

PERIOD_MONTHS = {
    "monthly": 1,
    "bimonthly": 2,  # billed every two months (some property-tax bills)
    "quarterly": 3,
    "semiannual": 6,
    "annual": 12,
}


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


def _rent(unit: dict) -> float | None:
    return _num(unit, "rent_verified", "rent_gross", "price")


def _flag(flag: str, action: str, meaning: str, **evidence) -> dict:
    out = {"flag": flag, "action": action, "meaning": meaning}
    out.update(evidence)
    return out


# ------------------------------------------------------------------ fees

def normalize_fee(fee: dict) -> dict:
    """{type, amount, currency, billing_period, source_url, observed_at}
    -> adds `monthly` (amount spread per month) or `monthly: None` with a
    reason. A placeholder amount (999, 9999, ...) is "not stated", never a
    number. one_time fees have no monthly figure here (costs.py amortizes
    them)."""
    out = dict(fee)
    amount = fee.get("amount")
    period = (fee.get("billing_period") or "").lower() or None
    if amount is None:
        out.update(monthly=None, status="MISSING", why="amount not stated")
    elif PLACEHOLDER_FEE.match(str(int(amount)) if isinstance(amount, (int, float)) else str(amount)):
        out.update(amount=None, monthly=None, status="MISSING",
                   why=f"placeholder value {amount} treated as not stated")
    elif period == "one_time":
        out.update(monthly=None, status="FACT", why="one-time fee; amortize in costs.py, never add as monthly")
    elif period in PERIOD_MONTHS:
        out.update(monthly=round(float(amount) / PERIOD_MONTHS[period], 2), status="FACT")
    else:
        out.update(monthly=None, status="MISSING",
                   why="billing period unknown; never assume monthly")
    return out


def known_monthly_total(unit: dict) -> dict:
    """Rent + every recurring fee with a known monthly figure. `partial`
    is True whenever any listed fee could not be converted, and `missing`
    names them - a partial total is never presented as the full cost."""
    rent = _rent(unit)
    fees = [normalize_fee(f) for f in unit.get("fees") or []]
    known = [f for f in fees if f.get("monthly") is not None]
    missing = [f.get("type") or "fee" for f in fees
               if f.get("monthly") is None and (f.get("billing_period") or "").lower() != "one_time"]
    total = None if rent is None else round(rent + sum(f["monthly"] for f in known), 2)
    return {
        "rent": rent,
        "fees": [{"type": f.get("type"), "monthly": f["monthly"], "billing_period": f.get("billing_period")} for f in known],
        "total": total,
        "partial": rent is None or bool(missing),
        "missing": missing if rent is not None else ["rent"] + missing,
    }


# ------------------------------------------------------------ freshness

def parse_published(value: str | None, source_tz: str | None = None) -> _dt.datetime | None:
    """Parse a publication timestamp. A naive value gets source_tz only
    when the caller KNOWS the source's timezone; otherwise None."""
    if not value:
        return None
    try:
        t = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        if not source_tz:
            return None
        t = t.replace(tzinfo=ZoneInfo(source_tz))
    return t


def freshness(unit: dict, now: str, source_tz: str | None = None) -> dict:
    """Age of a listing since ORIGINAL publication (never updated_at)."""
    pub = parse_published(unit.get("published_at"), source_tz or unit.get("publication_timezone"))
    if pub is None:
        why = "no published_at" if not unit.get("published_at") else "published_at has no timezone and none is known"
        return {"hours": None, "why": why}
    now_t = _dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
    hours = (now_t - pub).total_seconds() / 3600
    return {"hours": round(hours, 1), "published_at": pub.isoformat()}


# ---------------------------------------------------------------- traps

def traps(unit: dict, config: dict | None = None, comps: list[dict] | None = None,
          now: str | None = None) -> list[dict]:
    config = config or {}
    budget = config.get("budget") or {}
    out: list[dict] = []
    rent = _rent(unit)

    if rent is None or rent <= 0:
        out.append(_flag("price_unstated", "skip", "rent not stated; nothing to compare"))
        return out

    whole_only = (config.get("unit") or {}).get("whole_unit_only", True)
    ptype = " ".join(str(_val(unit, k) or "") for k in ("property_type", "title", "unit_type"))
    if whole_only and SHARED_PATTERNS.search(ptype):
        out.append(_flag("shared_or_sublet", "skip",
                         "a room, shared unit, or short-term sublet, not a whole-unit rental",
                         text=ptype.strip()))

    floor = budget.get("gross_min")
    if floor and rent < floor:
        out.append(_flag("below_floor", "no_star",
                         "below the configured floor: often a room in a shared unit, or bait; open the photos",
                         rent=rent, floor=floor))

    beds = _num(unit, "beds")
    if comps:
        same = [_rent(c) for c in comps if _num(c, "beds") == beds and c is not unit]
        same = [p for p in same if p]
        if len(same) >= 3:
            med = statistics.median(same)
            if rent < med * FAR_BELOW_COMPS:
                out.append(_flag("far_below_comps", "no_star",
                                 "far below comparable units; shared unit, bait, or scam until proven otherwise",
                                 rent=rent, comps_median=med, comps_n=len(same)))

    sqft = _num(unit, "sqft")
    if sqft is None and _num(unit, "size_sqm") is not None:
        sqft = _num(unit, "size_sqm") / SQM_PER_SQFT
    if sqft is not None and beds is not None:
        typical = SQFT_TYPICAL.get(int(min(beds, 4)))
        if typical and (sqft > typical * 2 or sqft < typical * 0.5):
            out.append(_flag("implausible_size", "no_star",
                             "size far outside the normal range for this bed count: lot/garden area, a typo, or wrong beds",
                             sqft=round(sqft), typical=typical))

    for fee in unit.get("fees") or []:
        amt = fee.get("amount")
        if amt is not None and PLACEHOLDER_FEE.match(str(int(amt)) if isinstance(amt, (int, float)) else str(amt)):
            out.append(_flag("placeholder_fee", "warn",
                             f"{fee.get('type') or 'fee'} listed as {amt}: a placeholder, treated as not stated"))

    if now:
        f = freshness(unit, now)
        if f.get("hours") is not None and f["hours"] > STALE_DAYS * 24:
            out.append(_flag("stale_publication", "no_star",
                             "originally published over a year ago: recycled or abandoned ad",
                             published_at=f["published_at"]))
    pub = parse_published(unit.get("published_at"), unit.get("publication_timezone"))
    upd = parse_published(unit.get("updated_at"), unit.get("publication_timezone"))
    if pub and upd and (upd - pub).days >= BUMP_GAP_DAYS:
        out.append(_flag("bumped_listing", "info",
                         f"refreshed {(upd - pub).days} days after first posting; bumped to climb the feed, likely not renting",
                         published_at=pub.isoformat(), updated_at=upd.isoformat()))

    if float(rent).is_integer() and int(rent) % 5 != 0:
        out.append(_flag("odd_price", "info",
                         "non-round rent: the owner computed it; a serious ad, less room to haggle"))

    gmax = budget.get("gross_max")
    if gmax:
        total = known_monthly_total(unit)
        if total["total"] is not None and total["total"] > gmax and rent <= gmax:
            out.append(_flag("over_budget_all_in", "warn",
                             f"rent fits, but rent plus known fees is {total['total']:g}, over the {gmax:g} ceiling; lead the notes with this",
                             total=total["total"]))
        verified = _num(unit, "rent_verified")
        if (verified is not None and verified > gmax
                and "concession_badged_over_ceiling" in _flag_names(unit.get("quality_flags"))):
            out.append(_flag("over_budget_all_in", "warn",
                             f"opened on a concession badge, but the verified rent {verified:g} is over the "
                             f"{gmax:g} ceiling; lead the notes with this",
                             total=verified))

    if unit.get("rent_is_net") and _net_without_gross(unit.get("notes")):
        out.append(_flag("net_without_gross", "warn",
                         "note states a net figure without the gross beside it"))
    return out


def _flag_names(flags) -> set[str]:
    names = set()
    for f in flags or []:
        names.add(f.get("flag") if isinstance(f, dict) else f)
    return names


def worst_action(flags: list[dict]) -> str | None:
    order = ["skip", "no_star", "warn", "info"]
    actions = [f["action"] for f in flags]
    for a in order:
        if a in actions:
            return a
    return None


# ------------------------------------------------------------- feed cards

def concession_badge(text: str | None) -> bool:
    """True when a feed card's text carries a concession or starting-at
    badge (CONCESSION_BADGE)."""
    return bool(CONCESSION_BADGE.search(text or ""))


def feed_price_decision(card_price, card_text, budget: dict) -> dict:
    """The feed-first band check for one card.

    pass  - card price within gross_max
    open  - over gross_max but within gross_max_stretch (or gross_max when
            no stretch is set) AND the card carries a concession badge: the
            card price is a teaser, the page decides. Also any card with no
            price at all.
    skip  - over the ceiling otherwise, including badged cards above the
            stretch ceiling.
    """
    budget = budget or {}
    if card_price is None:
        return {"action": "open", "reason": "price_unstated_on_card"}
    gmax = budget.get("gross_max")
    if gmax is None or card_price <= gmax:
        return {"action": "pass"}
    stretch = budget.get("gross_max_stretch") or gmax
    if card_price <= stretch and concession_badge(card_text):
        return {"action": "open", "reason": "concession_badged_over_ceiling"}
    return {"action": "skip", "reason": "over_ceiling"}


def keyword_filter(text: str | None, filters: dict | None) -> dict:
    """Apply config `filters` to a feed card's title + description.

    Any `exclude_keywords` hit skips the card; a non-empty
    `include_keywords` list needs at least one hit. Keywords match as whole
    words or whole phrases, case-insensitive ("garden" does not match
    "gardening"). Empty or missing filters always pass. A skip is
    criteria-dependent: changing the keywords re-opens it.
    """
    filters = filters or {}
    text = (text or "").lower()

    def hit(word: str) -> bool:
        phrase = r"\s+".join(re.escape(w) for w in str(word).lower().split())
        return bool(phrase) and re.search(rf"\b{phrase}\b", text) is not None

    for word in filters.get("exclude_keywords") or []:
        if hit(word):
            return {"action": "skip", "reason": f"exclude_keyword:{word}"}
    include = [w for w in filters.get("include_keywords") or [] if str(w).strip()]
    if include and not any(hit(w) for w in include):
        return {"action": "skip", "reason": "no_include_keyword"}
    return {"action": "pass"}


# ----------------------------------------------------------- note hygiene

def relative_time_hits(units: list[dict]) -> list[dict]:
    """Units whose notes use relative time ('posted today', 'hurry').
    Sweep every round: such notes are true for one day, wrong forever."""
    hits = []
    for u in units:
        for field in ("notes", "value_note"):
            text = u.get(field) or ""
            found = sorted({m.group(0) for m in RELATIVE_TIME.finditer(text)})
            if found:
                hits.append({"unit_id": u.get("unit_id") or u.get("id"), "field": field, "phrases": found})
    return hits


def _net_without_gross(text: str | None) -> bool:
    text = text or ""
    return bool(NET_WITHOUT_GROSS.search(text)) and "gross" not in text.lower()


def net_without_gross_hits(units: list[dict]) -> list[dict]:
    """Units whose notes state a net figure with no gross beside it. A pure
    string check, no money math: the fix is to write "net $X / gross $Y"."""
    hits = []
    for u in units:
        text = u.get("notes") or ""
        if _net_without_gross(text):
            m = NET_WITHOUT_GROSS.search(text)
            start = max(m.start() - 40, 0)
            hits.append({"unit_id": u.get("unit_id") or u.get("id"),
                         "note_excerpt": text[start:m.end() + 40].strip()})
    return hits


# -------------------------------------------------------------- integrity

def integrity_report(units: list[dict], config: dict | None = None, root: str = ".") -> dict:
    """Pre-ship check: duplicate ids, missing local evidence files,
    out-of-spec rows, and same-unit pairs left unmerged."""
    import dedupe  # local module

    config = config or {}
    ids = [str(u.get("unit_id") or u.get("id") or "") for u in units]
    dup_ids = sorted({i for i in ids if i and ids.count(i) > 1})
    missing_ids = sum(1 for i in ids if not i)
    missing_files = []
    for u in units:
        for key in ("screenshot", "image"):
            path = u.get(key)
            if path and not str(path).startswith(("http://", "https://")):
                if not (pathlib.Path(root) / path).exists():
                    missing_files.append({"unit_id": u.get("unit_id") or u.get("id"), "field": key, "path": path})
    budget = config.get("budget") or {}
    beds_cfg = (config.get("unit") or {}).get("beds")
    out_of_spec = []
    for u in units:
        if u.get("live") is False:
            continue
        rent = _rent(u)
        why = []
        if rent is not None and budget.get("gross_max") and rent > budget.get("gross_max_stretch", budget["gross_max"]):
            why.append("over budget")
        if rent is not None and budget.get("gross_min") and rent < budget["gross_min"]:
            why.append("under floor")
        if beds_cfg is not None and _num(u, "beds") is not None and _num(u, "beds") < beds_cfg:
            why.append("too few beds")
        if why:
            out_of_spec.append({"unit_id": u.get("unit_id") or u.get("id"), "why": why})
    pairs = []
    for i, a in enumerate(units):
        for b in units[i + 1:]:
            level = dedupe.match(a, b)
            if level:
                pairs.append({"a": a.get("unit_id") or a.get("id"), "b": b.get("unit_id") or b.get("id"), "level": level})
    return {
        "total": len(units),
        "duplicate_ids": dup_ids,
        "missing_ids": missing_ids,
        "missing_files": missing_files,
        "out_of_spec": out_of_spec,
        "same_unit_pairs": pairs,
        "relative_time_notes": relative_time_hits(units),
        "net_without_gross": net_without_gross_hits(units),
        "ok": not (dup_ids or missing_ids or missing_files or out_of_spec),
    }


def _load(path: str):
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    return list(data.values()) if isinstance(data, dict) else data


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print(__doc__)
        return 2
    cmd, units = args[0], _load(args[1])
    config = json.loads(pathlib.Path(args[2]).read_text(encoding="utf-8")) if len(args) > 2 else {}
    if cmd == "traps":
        now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        rows = [{"unit_id": u.get("unit_id") or u.get("id"), "flags": traps(u, config, units, now)} for u in units]
        print(json.dumps([r for r in rows if r["flags"]], indent=2, ensure_ascii=False))
        return 0
    if cmd == "notes":
        hits = relative_time_hits(units)
        print(json.dumps(hits, indent=2, ensure_ascii=False))
        return 1 if hits else 0
    if cmd == "integrity":
        report = integrity_report(units, config)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["ok"] else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
