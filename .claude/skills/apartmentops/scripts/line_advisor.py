#!/usr/bin/env python3
"""Intra-building line-substitution advisor.

Towers repeat floor plans in vertical lines: unit 1205 and unit 1805 are
often the same floor plan and orientation, six floors apart, just with
different rent. This module cross-joins three plain files already in the
data contract:

1. a hand-curated per-building line map (apartmentops/data/lines/{slug}.yml
   - schema documented in references/line-substitution.md), giving each
   line's orientation and confirming which lines actually exist;
2. live and historical units in apartmentops/data/verified.json;
3. per-unit price/status trajectories, as produced by the Market memory
   epic's snapshot-ledger trajectory extraction (a plain dict keyed by
   unit_id, each value an ordered list of {price, status, observed_at}
   observations).

Orientation and line identity claims come ONLY from the line map's cited
source (an official floor plan or key plan URL recorded in the map file
itself) - never from inference over unit numbers, photos, or floor-plan
images. A trailing letter in a unit string is only trusted as a line when
the curated map confirms that line exists; an unmapped building produces
no output at all, silently - never a guessed line.

This module performs local computation only: it reads plain files/dicts
already in memory and writes plain dicts. It never fetches a URL, never
contacts a broker or landlord, and never auto-sends anything - a human
acts on any suggestion via the sibling unit's own deep link.

Library usage (import directly - no network, no I/O beyond what you pass
in):
    from line_advisor import unit_line, suggest, build_lines_index

CLI usage (reads/writes real files):
    python3 line_advisor.py build verified.json lines_dir trajectories.json > lines.json

Where lines_dir contains one YAML file per building
(apartmentops/data/lines/{building-slug}.yml) and trajectories.json is the
{unit_id: [{price, status, observed_at}, ...]} series from the snapshot
ledger's trajectory extraction.

Requires (CLI `build` command only): pip install PyYAML
(PyYAML is imported lazily; only the YAML-loading path requires it. The
library functions unit_line()/suggest()/build_lines_index() take plain
dicts and need no third-party dependency at all.)
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only when PyYAML is absent
    yaml = None  # type: ignore[assignment]

_YAML_HINT = "PyYAML is required to load line-map YAML files. Install it with: pip install PyYAML"

DEFAULT_WINDOW_MONTHS = 12
MIN_PRICED_OBSERVATIONS = 2


def _require_yaml() -> None:
    if yaml is None:
        raise ImportError(_YAML_HINT)


# --------------------------------------------------------------------------
# Line identity
# --------------------------------------------------------------------------

_TRAILING_LETTERS = re.compile(r"([A-Za-z]+)\s*$")


def unit_line(unit_str: str | None, line_map: dict | None) -> str | None:
    """Resolve a unit string to a curated line name, or None if it cannot
    be confirmed.

    Resolution order (first confirmed match wins; nothing is ever guessed):
      1. An explicit unit -> line override in line_map["units"][unit_str],
         but ONLY if that line also exists in line_map["lines"].
      2. Otherwise, the trailing run of letters in unit_str (e.g. "1205C"
         -> "C", "12-PH-C" -> "C"), matched case-insensitively against the
         keys of line_map["lines"]. Returns the map's own canonical key
         (preserving its case), not the raw trailing letters.
      3. Otherwise None.

    A building with no line map, or a unit whose trailing letters do not
    match any line the map confirms, both return None - the caller must
    treat None as "no advisor output for this unit", never as "guess C".
    """
    if not line_map or not unit_str:
        return None

    lines = line_map.get("lines") or {}
    overrides = line_map.get("units") or {}

    if unit_str in overrides:
        override_line = overrides[unit_str]
        return override_line if override_line in lines else None

    match = _TRAILING_LETTERS.search(str(unit_str))
    if not match:
        return None
    letters = match.group(1).upper()
    for key in lines:
        if str(key).upper() == letters:
            return key
    return None


# --------------------------------------------------------------------------
# Price/floor helpers
# --------------------------------------------------------------------------


def _price_of(row: dict) -> float | int | None:
    """Read a price off a verified_units row, tolerating the field-name
    differences between this ticket's candidate schema ("price") and the
    existing contracts.md verified.json schema ("rent_verified")."""
    for key in ("price", "rent_verified", "rent_gross"):
        value = row.get(key)
        if value is not None:
            return value
    return None


def _parse_dt(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Per-candidate suggestion
# --------------------------------------------------------------------------


def suggest(
    candidate: dict,
    verified_units: list[dict],
    line_map: dict | None,
    trajectories: dict,
) -> dict:
    """Cross-join one live candidate against its building's other units in
    the same curated line, plus that line's price/trajectory history.

    candidate: {unit_id, building, unit, price}
    verified_units: every known unit row for this building (live AND
        historical/gone rows - contracts.md's verified.json never deletes
        gone units, and line history needs that record).  Rows are matched
        to candidate by row["building"] == candidate["building"]; price is
        read via the same tolerant lookup as verified.json's real field
        names (see _price_of).
    line_map: this building's curated line map dict (see
        references/line-substitution.md), or None/{} if this building has
        no curated map.
    trajectories: {unit_id: [{price, status, observed_at}, ...]} - already
        restricted to whatever window the caller wants reflected in
        line_history (build_lines_index applies the 12-month window before
        calling this; call this directly with a pre-filtered dict for full
        control, e.g. in tests).

    Returns {"siblings": [...], "line_history": {...} or None}.

    siblings: [{unit_id, floor_delta, price_delta, same_line, live}, ...],
    one entry per OTHER unit in the same building resolved to the same
    line as candidate. floor_delta and price_delta are (sibling - candidate)
    signed values, or None if either side's floor/price is unknown - never
    fabricated. Live, cheaper siblings sort first; ties broken by cheapest
    price_delta.

    line_history: {"times_listed": int, "price_min": num, "price_max": num}
    computed across every priced trajectory observation, for every unit in
    the line (siblings + candidate), within whatever window the caller
    passed in `trajectories`. "times_listed" counts distinct listing
    stints (a transition into status "live" from anything else, including
    the first-ever observation for a unit) - not raw re-check counts.
    Returns None when a line map/line cannot be resolved at all, OR when
    fewer than 2 priced observations exist in the line's history (an
    "insufficient ledger data" state the caller/dashboard renders
    explicitly - see references/line-substitution.md). Callers should not
    conflate "None because no line map" with "None because insufficient
    history"; build_lines_index only emits an index entry at all once a
    line map and line ARE resolved, so within that context a None
    line_history unambiguously means insufficient history.
    """
    empty = {"siblings": [], "line_history": None}
    if not line_map:
        return empty

    building = candidate.get("building")
    line = unit_line(candidate.get("unit"), line_map)
    if line is None:
        return empty

    candidate_unit_id = candidate.get("unit_id")
    candidate_price = candidate.get("price")

    line_rows = []
    candidate_floor = None
    for row in verified_units:
        if row.get("building") != building:
            continue
        if unit_line(row.get("unit"), line_map) != line:
            continue
        line_rows.append(row)
        if row.get("unit_id") == candidate_unit_id and candidate_floor is None:
            candidate_floor = row.get("floor")

    siblings = []
    for row in line_rows:
        row_unit_id = row.get("unit_id")
        if row_unit_id == candidate_unit_id:
            continue
        sibling_floor = row.get("floor")
        sibling_price = _price_of(row)
        floor_delta = (
            sibling_floor - candidate_floor
            if isinstance(sibling_floor, (int, float)) and isinstance(candidate_floor, (int, float))
            else None
        )
        price_delta = (
            sibling_price - candidate_price
            if isinstance(sibling_price, (int, float)) and isinstance(candidate_price, (int, float))
            else None
        )
        siblings.append(
            {
                "unit_id": row_unit_id,
                "floor_delta": floor_delta,
                "price_delta": price_delta,
                "same_line": True,
                "live": bool(row.get("live")),
            }
        )

    def sort_key(sibling: dict) -> tuple:
        is_cheaper_live = (
            sibling["live"]
            and sibling["price_delta"] is not None
            and sibling["price_delta"] < 0
        )
        price_rank = sibling["price_delta"] if sibling["price_delta"] is not None else float("inf")
        return (0 if is_cheaper_live else 1, price_rank, str(sibling["unit_id"]))

    siblings.sort(key=sort_key)

    all_prices: list[float] = []
    times_listed = 0
    for row in line_rows:
        row_unit_id = row.get("unit_id")
        observations = list(trajectories.get(row_unit_id, [])) if trajectories else []
        observations.sort(key=lambda o: o.get("observed_at") or "")
        prev_status = None
        for obs in observations:
            price = obs.get("price")
            status = obs.get("status")
            if isinstance(price, (int, float)):
                all_prices.append(price)
            if status == "live" and prev_status != "live":
                times_listed += 1
            prev_status = status

    if len(all_prices) < MIN_PRICED_OBSERVATIONS:
        line_history = None
    else:
        line_history = {
            "times_listed": times_listed,
            "price_min": min(all_prices),
            "price_max": max(all_prices),
        }

    return {"siblings": siblings, "line_history": line_history}


# --------------------------------------------------------------------------
# Trajectory windowing + batch driver
# --------------------------------------------------------------------------


def recent_observations(
    observations: list[dict],
    months: int = DEFAULT_WINDOW_MONTHS,
    now: datetime.datetime | None = None,
) -> list[dict]:
    """Restrict one unit's trajectory series to the trailing `months`
    window (approximated as 30 days/month, documented here rather than
    silently baked in). Observations with no parseable observed_at are
    excluded - never guessed into or out of the window."""
    now_dt = now or datetime.datetime.now(datetime.timezone.utc)
    window_start = now_dt - datetime.timedelta(days=30 * months)
    kept = []
    for obs in observations:
        ts = _parse_dt(obs.get("observed_at"))
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        if window_start <= ts <= now_dt:
            kept.append(obs)
    return kept


def recent_observations_map(
    trajectories: dict,
    months: int = DEFAULT_WINDOW_MONTHS,
    now: datetime.datetime | None = None,
) -> dict:
    return {
        unit_id: recent_observations(obs, months=months, now=now)
        for unit_id, obs in (trajectories or {}).items()
    }


def build_lines_index(
    verified_units: list[dict],
    line_maps_by_building: dict,
    trajectories: dict,
    months: int = DEFAULT_WINDOW_MONTHS,
    now: datetime.datetime | None = None,
) -> tuple[dict, dict]:
    """Batch driver: run suggest() for every live unit whose building has a
    curated line map and whose unit number resolves to a known line.

    line_maps_by_building: {building_name: line_map_dict}, keyed by the
        line map's OWN "building" field (not the filename slug) so joining
        against verified_units rows never depends on slug-guessing. Use
        load_all_line_maps() to build this from a directory of YAML files.

    Returns (index, report):
        index: {unit_id: suggest()-result}, one entry per unit that
            resolved to a known line in a mapped building. Buildings with
            no line map, and units whose number does not resolve to a
            known line, are OMITTED entirely - not present with an empty
            placeholder - so "no line map" stays silent per the design.
        report: counts for the run report -
            {"live_candidates", "no_line_map", "unparsed_unit", "included"}
    """
    windowed_trajectories = recent_observations_map(trajectories, months=months, now=now)

    index: dict[str, dict] = {}
    report = {
        "live_candidates": 0,
        "no_line_map": 0,
        "unparsed_unit": 0,
        "included": 0,
    }

    for row in verified_units:
        if not row.get("live"):
            continue
        report["live_candidates"] += 1

        building = row.get("building")
        line_map = line_maps_by_building.get(building)
        if not line_map:
            report["no_line_map"] += 1
            continue

        line = unit_line(row.get("unit"), line_map)
        if line is None:
            report["unparsed_unit"] += 1
            continue

        unit_id = row.get("unit_id")
        candidate = {
            "unit_id": unit_id,
            "building": building,
            "unit": row.get("unit"),
            "price": _price_of(row),
        }
        index[unit_id] = suggest(candidate, verified_units, line_map, windowed_trajectories)
        report["included"] += 1

    return index, report


# --------------------------------------------------------------------------
# YAML line-map loading (CLI path only)
# --------------------------------------------------------------------------


def load_line_map(path: str | pathlib.Path) -> dict:
    """Load one building's line map YAML file. See
    references/line-substitution.md for the schema."""
    _require_yaml()
    text = pathlib.Path(path).read_text()
    return yaml.safe_load(text) or {}


def load_all_line_maps(directory: str | pathlib.Path) -> dict:
    """Load every *.yml file in `directory`, keyed by each file's own
    "building" field (see build_lines_index for why the join key is the
    field, not the filename slug). Files with no "building" field are
    skipped - never guessed into the index under a synthesized name."""
    maps: dict[str, dict] = {}
    for path in sorted(pathlib.Path(directory).glob("*.yml")):
        line_map = load_line_map(path)
        name = line_map.get("building")
        if name:
            maps[name] = line_map
    return maps


def main() -> int:
    if len(sys.argv) != 5 or sys.argv[1] != "build":
        print(
            "usage: line_advisor.py build verified.json lines_dir trajectories.json",
            file=sys.stderr,
        )
        return 2

    verified_path, lines_dir, trajectories_path = sys.argv[2], sys.argv[3], sys.argv[4]

    verified_units = json.loads(pathlib.Path(verified_path).read_text())
    line_maps_by_building = load_all_line_maps(lines_dir)
    trajectories = json.loads(pathlib.Path(trajectories_path).read_text())

    index, report = build_lines_index(verified_units, line_maps_by_building, trajectories)
    json.dump({"lines": index, "report": report}, sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
