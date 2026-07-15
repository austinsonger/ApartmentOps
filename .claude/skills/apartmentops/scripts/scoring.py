#!/usr/bin/env python3
"""Spec-driven unit scoring: composite, action band, distress, climate, ask.

Everything here reads a committed spec (apartmentops/scoring.yml, mirrored
in examples/scoring.example.yml) and turns per-unit inputs into
deterministic, reproducible outputs. Nothing here decides what a raw
dimension score IS - that is the research subagent's job, constrained by
the spec's verbal anchor bands (see references/scoring.md). This module
only combines already-scored dimensions, ledger-derived events, and
climate stats into a composite, an action band, distress flags, a
concession-climate classification, and a negotiation anchor.

Usage:
    python3 scoring.py scoring.yml dim_scores.json

    dim_scores.json: {"commute": 18, "budget_fit": 14, "safety": null, ...}
    Prints the score_unit() result as JSON.

Library usage (all pure functions, no I/O except load_spec):
    from scoring import load_spec, score_unit, band_with_gates, calibrate
    from scoring import distress_flags, concession_climate, negotiation_ask
    from scoring import move_in_fit_score

    spec = load_spec("apartmentops/scoring.yml")
    scored = score_unit({"commute": 18, "budget_fit": 14, ...}, spec)
    band = band_with_gates(scored["band"], tour_now_blocked=has_unknown_gate)

Anti-fabrication rule, restated here because this module is the one place
it becomes code: a dimension with no groundable evidence is None, never a
guessed midpoint. negotiation_ask() builds its justification strings only
from the unit/climate/comps data it is given - it never invents a reason.
The negotiation "ask" this module returns is a number for the user to say
in person or type themselves; nothing in this module or its callers sends,
drafts-and-sends, or submits anything.

Requires PyYAML for load_spec() only (guarded import - all other functions
are dependency-free stdlib).
"""

from __future__ import annotations

import datetime
import json
import math
import pathlib
import sys

TOUR_NOW = "TourNow"
WATCH = "Watch"
SKIP = "Skip"
UNSCORED = "n/a - unscored"

DEFAULT_STALE_FACTOR = 1.5
DEFAULT_MIN_SAMPLE = 5
DEFAULT_CLIMATE_THRESHOLDS = {
    "cut_share_renter_favorable": 0.5,
    "concession_rate_renter_favorable": 0.4,
    "cut_share_landlord_favorable": 0.15,
    "concession_rate_landlord_favorable": 0.1,
}

# move_in_fit_score() day-gap thresholds (documented in scoring.yml under
# move_in_fit:, overridable there - see references/scoring.md section 8).
DEFAULT_MOVE_IN_PERFECT_MAX_DAYS = 0
DEFAULT_MOVE_IN_MINOR_MAX_DAYS = 14
DEFAULT_MOVE_IN_MODERATE_MAX_DAYS = 42


# ---------------------------------------------------------------------------
# Spec loading and validation
# ---------------------------------------------------------------------------


def load_spec(path) -> dict:
    """Load and validate a scoring.yml spec. Raises ValueError if invalid.

    Validates:
    - every dimension has a weight, and weights sum to 1.0 (tolerance 1e-6)
    - every dimension's anchor bands tile its range with no gaps or overlaps
    - action_bands defines tour_now_min > watch_min
    """
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required to load scoring specs. Install it with: "
            "pip install pyyaml"
        ) from exc

    text = pathlib.Path(path).read_text()
    spec = yaml.safe_load(text)
    _validate_spec(spec)
    return spec


def _validate_spec(spec) -> None:
    if not isinstance(spec, dict):
        raise ValueError("scoring spec must be a mapping")

    dims = spec.get("dimensions")
    if not dims or not isinstance(dims, dict):
        raise ValueError("scoring spec must define at least one dimension")

    total_weight = 0.0
    for name, dim in dims.items():
        weight = dim.get("weight") if isinstance(dim, dict) else None
        if weight is None:
            raise ValueError(f"dimension {name!r} is missing a weight")
        total_weight += weight

        anchors = dim.get("anchors")
        if not anchors:
            raise ValueError(f"dimension {name!r} has no anchor bands")

        sorted_anchors = sorted(anchors, key=lambda a: a["score_min"])
        for anchor in sorted_anchors:
            if anchor["score_min"] > anchor["score_max"]:
                raise ValueError(
                    f"dimension {name!r} has an anchor with score_min > "
                    f"score_max: {anchor}"
                )
        for prev, cur in zip(sorted_anchors, sorted_anchors[1:]):
            if cur["score_min"] <= prev["score_max"]:
                raise ValueError(
                    f"dimension {name!r} anchor bands overlap: "
                    f"{prev} and {cur}"
                )
            if cur["score_min"] > prev["score_max"] + 1:
                raise ValueError(
                    f"dimension {name!r} anchor bands have a gap between "
                    f"{prev} and {cur}"
                )

    if not math.isclose(total_weight, 1.0, abs_tol=1e-6):
        raise ValueError(f"dimension weights must sum to 1.0, got {total_weight}")

    action_bands = spec.get("action_bands")
    if not action_bands or "tour_now_min" not in action_bands or "watch_min" not in action_bands:
        raise ValueError(
            "scoring spec must define action_bands.tour_now_min and watch_min"
        )
    if action_bands["tour_now_min"] <= action_bands["watch_min"]:
        raise ValueError("action_bands.tour_now_min must be greater than watch_min")


# ---------------------------------------------------------------------------
# Composite scoring
# ---------------------------------------------------------------------------


def _anchor_index(value: float, sorted_anchors: list[dict]) -> int:
    """Return the index of the anchor band `value` falls in.

    _validate_spec() permits a gap of up to 1 between adjacent integer
    anchor bounds (e.g. one band ending at score_max=16, the next starting
    at score_min=17) so that whole-number anchors can tile an integer
    range with clean, non-overlapping text like "13-16" / "17-20". That
    gap is inert for integer scores - every integer still lands in exactly
    one band - but dim_scores is typed float | None (subagents return "a
    number", and callers may pass any float), so a fractional score such
    as 16.5 can land inside one of those permitted gaps with no exact
    match above.

    A prior version of this function treated that case as unreachable and
    raised ValueError, which crashed the whole scoring run on an entirely
    plausible input. Instead, snap to the nearer band by distance from
    `value` to the band's [score_min, score_max] interval. Ties (value
    sits exactly at a gap's midpoint, only possible when the gap is
    exactly 1) resolve to the higher-scoring, more-favorable band - a
    documented, deterministic rule, not a guessed value: the band index
    changes, never the caller-supplied score itself.

    Called only after the value has already been clamped to
    [dim_min, dim_max] in _dimension_fraction, so this never has to
    extrapolate past the tiled range's outer edges.
    """
    for i, anchor in enumerate(sorted_anchors):
        if anchor["score_min"] <= value <= anchor["score_max"]:
            return i

    best_i = 0
    best_distance = None
    for i, anchor in enumerate(sorted_anchors):
        if value < anchor["score_min"]:
            distance = anchor["score_min"] - value
        else:  # value > anchor["score_max"]; exact matches already returned above
            distance = value - anchor["score_max"]
        if best_distance is None or distance <= best_distance:
            best_distance = distance
            best_i = i
    return best_i


def _dimension_fraction(value: float, dim: dict) -> float:
    """Map a raw dimension score to a 0.0-1.0 fraction of that dimension.

    Non-rank_based dimensions: linear fraction of the tiled numeric range
    (dim_min..dim_max across all anchors), so magnitude matters.

    rank_based dimensions: the fraction is the matched anchor's ordinal
    position among the sorted tiers (0.0 for the worst tier, 1.0 for the
    best), so one extreme raw number cannot dominate the composite - only
    which tier it lands in matters, not how far into that tier it sits.
    """
    anchors = dim.get("anchors", [])
    sorted_anchors = sorted(anchors, key=lambda a: a["score_min"])
    dim_min = sorted_anchors[0]["score_min"]
    dim_max = sorted_anchors[-1]["score_max"]
    clamped = min(max(value, dim_min), dim_max)
    idx = _anchor_index(clamped, sorted_anchors)

    if dim.get("rank_based"):
        if len(sorted_anchors) == 1:
            return 1.0
        return idx / (len(sorted_anchors) - 1)

    if dim_max == dim_min:
        return 1.0
    return (clamped - dim_min) / (dim_max - dim_min)


def _band_for(composite: float, action_bands: dict) -> str:
    if composite >= action_bands["tour_now_min"]:
        return TOUR_NOW
    if composite >= action_bands["watch_min"]:
        return WATCH
    return SKIP


def score_unit(dim_scores: dict, spec: dict) -> dict:
    """Composite a unit's per-dimension scores into a band, per spec.

    dim_scores: {dimension_name: raw_score | None}. A dimension absent
    from this dict is treated the same as an explicit None: missing.

    Returns:
        {"composite": float 0-100, "band": "TourNow"|"Watch"|"Skip",
         "renormalized": [missing dimension names],
         "renormalization_note": str}   # only present if any dim missing

    When every dimension is missing, returns:
        {"composite": None, "band": "n/a - unscored",
         "renormalized": [all dimension names], "renormalization_note": str}

    Missing-dimension weight is redistributed proportionally across the
    dimensions that ARE available (weight_i / sum(available weights)), so
    a partial score never silently favors or penalizes a unit relative to
    the spec's intent - and the redistribution is always recorded on the
    result, never applied invisibly.
    """
    dims = spec["dimensions"]
    names = list(dims.keys())
    available = [n for n in names if dim_scores.get(n) is not None]
    missing = [n for n in names if dim_scores.get(n) is None]

    if not available:
        return {
            "composite": None,
            "band": UNSCORED,
            "renormalized": missing,
            "renormalization_note": (
                f"scored on 0 of {len(names)} dimensions; no scoreable "
                "dimensions available"
            ),
        }

    total_weight = sum(dims[n]["weight"] for n in available)
    if total_weight <= 0:
        raise ValueError("available dimensions have zero total weight")

    composite_fraction = 0.0
    for name in available:
        dim = dims[name]
        fraction = _dimension_fraction(dim_scores[name], dim)
        renorm_weight = dim["weight"] / total_weight
        composite_fraction += fraction * renorm_weight

    composite = round(composite_fraction * 100, 1)
    band = _band_for(composite, spec["action_bands"])

    result = {"composite": composite, "band": band, "renormalized": missing}
    if missing:
        result["renormalization_note"] = (
            f"scored on {len(available)} of {len(names)} dimensions; "
            "weights renormalized"
        )
    return result


def band_with_gates(band: str, tour_now_blocked: bool) -> str:
    """Demote TourNow to Watch when a hard gate is UNKNOWN (or otherwise
    blocking). All other bands pass through unchanged.

    This is the seam where the tri-state hard-gate evaluator (any gate
    UNKNOWN -> tour_now_blocked=True) caps a unit out of TourNow regardless
    of how strong its composite score is.
    """
    if band == TOUR_NOW and tour_now_blocked:
        return WATCH
    return band


def calibrate(bands: list, spec: dict) -> dict:
    """Check whether TourNow's share of `bands` exceeds the spec's target.

    bands: a flat list of band strings (one per live, scored unit -
    typically after band_with_gates has already been applied).

    Returns {"share_tour_now": float, "ok": bool, "warning": str|None,
    "sample_size": int}. Threshold changes belong in scoring.yml
    (calibration.tour_now_max_share), never in code - this function only
    reads the value and reports, it never adjusts thresholds itself.
    """
    target = spec["calibration"]["tour_now_max_share"]
    total = len(bands)
    if total == 0:
        return {"share_tour_now": 0.0, "ok": True, "warning": None, "sample_size": 0}

    tour_now = sum(1 for b in bands if b == TOUR_NOW)
    share = tour_now / total
    ok = share <= target
    warning = None
    if not ok:
        warning = (
            f"TourNow share {share:.1%} exceeds target {target:.1%} "
            f"({tour_now} of {total} live units); consider raising "
            "action_bands.tour_now_min in scoring.yml"
        )
    return {
        "share_tour_now": round(share, 4),
        "ok": ok,
        "warning": warning,
        "sample_size": total,
    }


# ---------------------------------------------------------------------------
# Market-distress flags
# ---------------------------------------------------------------------------


def _flag_entry(flag_id: str, severity_table: dict) -> dict:
    row = severity_table.get(flag_id)
    if row is None:
        return {
            "flag": flag_id,
            "severity": None,
            "meaning": "no severity entry committed in scoring.yml for this flag",
        }
    return {
        "flag": flag_id,
        "severity": row.get("points"),
        "meaning": row.get("meaning", ""),
    }


def distress_flags(events: dict, spec: dict) -> list:
    """Compute negotiation-positive leverage flags from ledger-derived events.

    events: {"relist_count": int|None, "cuts_30d": int|None,
             "days_listed": int|None, "neighborhood_median_days": int|None}
    A field of None means "unknown" and simply cannot trigger its flag -
    it is the caller's job (reading the snapshot ledger) to render "no
    history yet" for a first-time-observed unit rather than treating an
    empty flag list here as evidence of a calm listing.

    Flags (all framed as leverage signals, never scam/fraud language):
    - relisted: relist_count >= 1
    - price_cuts_30d: cuts_30d >= 2
    - stale: days_listed > neighborhood_median_days * distress.stale_factor
      (stale_factor defaults to 1.5 if not set in spec)

    Returns a list of {"flag", "severity", "meaning"} using the committed
    severity table in spec["distress"]["severity"]; a flag with no matching
    table row still returns (severity=None) rather than being silently
    dropped, so a spec gap is visible instead of hidden.
    """
    distress_spec = spec.get("distress", {}) if spec else {}
    severity_table = {row["flag"]: row for row in distress_spec.get("severity", [])}
    stale_factor = distress_spec.get("stale_factor", DEFAULT_STALE_FACTOR)

    flags = []

    relist_count = events.get("relist_count")
    if relist_count is not None and relist_count >= 1:
        flags.append(_flag_entry("relisted", severity_table))

    cuts_30d = events.get("cuts_30d")
    if cuts_30d is not None and cuts_30d >= 2:
        flags.append(_flag_entry("price_cuts_30d", severity_table))

    days_listed = events.get("days_listed")
    median_days = events.get("neighborhood_median_days")
    if days_listed is not None and median_days is not None and median_days > 0:
        if days_listed > median_days * stale_factor:
            flags.append(_flag_entry("stale", severity_table))

    return flags


# ---------------------------------------------------------------------------
# Move-in-window fit
# ---------------------------------------------------------------------------


def _as_date(value) -> datetime.date:
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value))


def move_in_fit_score(
    available_date, window_start, window_end, spec: dict | None = None
) -> float | None:
    """Score a unit's available date against the user's move-in window.

    available_date, window_start, window_end: each either an ISO
    "YYYY-MM-DD" date string, a datetime.date, or None. window_start /
    window_end come from the caller reading config.yml's move-in window
    (this function does not read config.yml itself, matching the pattern
    of distress_flags()/concession_climate() taking caller-computed
    inputs).

    Returns a raw score in the move_in_window_fit dimension's tiled range
    (see the anchor bands committed in scoring.yml), or None - "n/a" - when
    available_date is missing (a listing with no stated availability),
    when window_start/window_end is missing (no move-in window configured),
    or when any of the three fails to parse as a date. None/unparseable is
    always n/a here, never a guessed midpoint and never a crash on a messy
    scraped date string.

    gap_days is 0 when available_date falls inside
    [window_start, window_end]; otherwise it is the number of days from
    available_date to the nearer window edge. gap_days is bucketed into
    one of four tiers using day thresholds read from
    spec["move_in_fit"] (perfect_max_days, minor_max_days,
    moderate_max_days - documented defaults 0/14/42, see
    references/scoring.md section 8), and the score returned is the top
    of that tier's anchor band (20, 16, 12, or 8) - the same tier
    boundaries already committed as the move_in_window_fit anchors, so the
    result always lands cleanly inside the band whose "meaning" text
    matches, with no invented in-between precision.
    """
    if available_date is None or window_start is None or window_end is None:
        return None

    try:
        available = _as_date(available_date)
        start = _as_date(window_start)
        end = _as_date(window_end)
    except (ValueError, TypeError):
        return None

    if start <= available <= end:
        gap_days = 0
    elif available < start:
        gap_days = (start - available).days
    else:
        gap_days = (available - end).days

    move_in_spec = spec.get("move_in_fit", {}) if spec else {}
    perfect_max = move_in_spec.get("perfect_max_days", DEFAULT_MOVE_IN_PERFECT_MAX_DAYS)
    minor_max = move_in_spec.get("minor_max_days", DEFAULT_MOVE_IN_MINOR_MAX_DAYS)
    moderate_max = move_in_spec.get("moderate_max_days", DEFAULT_MOVE_IN_MODERATE_MAX_DAYS)

    if gap_days <= perfect_max:
        return 20.0
    if gap_days <= minor_max:
        return 16.0
    if gap_days <= moderate_max:
        return 12.0
    return 8.0


# ---------------------------------------------------------------------------
# Concession-climate classification
# ---------------------------------------------------------------------------


def concession_climate(stats: dict, spec: dict) -> dict:
    """Classify a building or neighborhood's concession climate.

    stats: {"tracked": int, "with_cuts": int, "median_days_live": number|None,
            "concession_mentions": int}

    Below spec["climate"]["min_sample"] (default 5), returns
    {"classification": None, "reason": "sample below minimum",
     "sample_size": tracked, "min_sample": N, "disclosed": True} - the
     dashboard renders "insufficient sample (n=X)", never a low-n label.

    Otherwise returns {"classification": "landlord-favorable"|"neutral"|
    "renter-favorable", "sample_size", "cut_share", "median_days_live",
    "concession_rate", "disclosed": True}. Sample size is always present
    on the output ("disclosed": True) so a chip can never show a
    classification without its n.

    Thresholds (cut_share_renter_favorable, concession_rate_renter_favorable,
    cut_share_landlord_favorable, concession_rate_landlord_favorable) are
    read from spec["climate"] with documented defaults when the spec omits
    them; see references/scoring.md.
    """
    climate_spec = spec.get("climate", {}) if spec else {}
    min_sample = climate_spec.get("min_sample", DEFAULT_MIN_SAMPLE)
    tracked = stats.get("tracked") or 0

    if tracked < min_sample:
        return {
            "classification": None,
            "reason": "sample below minimum",
            "sample_size": tracked,
            "min_sample": min_sample,
            "disclosed": True,
        }

    with_cuts = stats.get("with_cuts") or 0
    concession_mentions = stats.get("concession_mentions") or 0
    cut_share = with_cuts / tracked
    concession_rate = concession_mentions / tracked

    renter_cut = climate_spec.get(
        "cut_share_renter_favorable", DEFAULT_CLIMATE_THRESHOLDS["cut_share_renter_favorable"]
    )
    renter_concession = climate_spec.get(
        "concession_rate_renter_favorable",
        DEFAULT_CLIMATE_THRESHOLDS["concession_rate_renter_favorable"],
    )
    landlord_cut = climate_spec.get(
        "cut_share_landlord_favorable",
        DEFAULT_CLIMATE_THRESHOLDS["cut_share_landlord_favorable"],
    )
    landlord_concession = climate_spec.get(
        "concession_rate_landlord_favorable",
        DEFAULT_CLIMATE_THRESHOLDS["concession_rate_landlord_favorable"],
    )

    if cut_share >= renter_cut or concession_rate >= renter_concession:
        classification = "renter-favorable"
    elif cut_share <= landlord_cut and concession_rate <= landlord_concession:
        classification = "landlord-favorable"
    else:
        classification = "neutral"

    return {
        "classification": classification,
        "sample_size": tracked,
        "cut_share": round(cut_share, 4),
        "median_days_live": stats.get("median_days_live"),
        "concession_rate": round(concession_rate, 4),
        "disclosed": True,
    }


# ---------------------------------------------------------------------------
# Negotiation anchor
# ---------------------------------------------------------------------------


def negotiation_ask(unit: dict, climate: dict, comps: list | None = None) -> dict:
    """Compute a numeric negotiation anchor: weeks free or dollars/month off.

    THIS IS A NUMBER THE USER SAYS IN PERSON OR TYPES THEMSELVES. Nothing
    in this function or any caller sends, drafts-and-sends, or submits an
    offer, a message, or a negotiation to anyone.

    unit: {"cuts": int|None, "days_listed": int|None, "current_price": number|None}
    climate: a concession_climate() result (needs "classification", "sample_size")
    comps: optional [{"note": str, "price": number}] - same-line/building
        units that are useful context. Only used if a comp price is
        strictly below the unit's current_price.

    Formula (documented in full in references/scoring.md, next to
    scoring.yml, so the anchor is reproducible from its inputs):
    - +1 week free per observed price cut
    - +1 week free if days_listed >= 30, +1 more if days_listed >= 60
    - +1 week free if climate is renter-favorable; -0.5 week (floor 0
      overall) if landlord-favorable; +0 if neutral
    - monthly_off = weeks_free * current_price / 52 (the dollar value of
      weeks_free spread evenly across a 12-month lease)

    Every justification string is built only from the unit/climate/comps
    values passed in - nothing is invented.

    When cuts, days_listed, current_price, or a real climate classification
    (not "insufficient sample") is missing, returns:
        {"ask": None, "justification": [], "insufficient_data": True,
         "reason": "insufficient data - no anchor computed",
         "missing": [list of what is missing]}
    """
    comps = comps or []
    missing = []

    cuts = unit.get("cuts")
    days_listed = unit.get("days_listed")
    current_price = unit.get("current_price")

    if cuts is None:
        missing.append("unit cuts count")
    if days_listed is None:
        missing.append("unit days_listed")
    if current_price is None:
        missing.append("unit current_price")
    if not climate or climate.get("classification") is None:
        missing.append("concession climate classification (insufficient sample)")

    if missing:
        return {
            "ask": None,
            "justification": [],
            "insufficient_data": True,
            "reason": "insufficient data - no anchor computed",
            "missing": missing,
        }

    weeks_free = 0.0
    justification = []

    if cuts >= 1:
        weeks_free += cuts * 1.0
        justification.append(
            f"{cuts} price cut{'s' if cuts != 1 else ''} observed"
        )

    if days_listed >= 30:
        weeks_free += 1.0
        if days_listed >= 60:
            weeks_free += 1.0
        justification.append(f"{days_listed} days listed")

    classification = climate.get("classification")
    sample_size = climate.get("sample_size")
    if classification == "renter-favorable":
        weeks_free += 1.0
    elif classification == "landlord-favorable":
        weeks_free -= 0.5
    justification.append(
        f"neighborhood concession climate: {classification} (n={sample_size})"
    )

    weeks_free = max(0.0, weeks_free)

    comp_prices = [c for c in comps if c.get("price") is not None]
    if comp_prices:
        lowest = min(comp_prices, key=lambda c: c["price"])
        if lowest["price"] < current_price:
            note = f" ({lowest['note']})" if lowest.get("note") else ""
            justification.append(f"comparable unit at ${lowest['price']:,.0f}{note}")

    weeks_free = round(weeks_free, 1)
    monthly_off = round(weeks_free * current_price / 52, 0)

    return {
        "ask": {"weeks_free": weeks_free, "monthly_off": monthly_off},
        "justification": justification,
        "insufficient_data": False,
    }


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: scoring.py SPEC_YAML DIM_SCORES_JSON", file=sys.stderr)
        return 2

    spec = load_spec(sys.argv[1])
    dim_scores = json.loads(pathlib.Path(sys.argv[2]).read_text())
    result = score_unit(dim_scores, spec)
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
