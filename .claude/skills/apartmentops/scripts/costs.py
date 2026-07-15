#!/usr/bin/env python3
"""Deterministic money math for ApartmentOps.

Every dollar figure ApartmentOps shows a user - net-effective rent, fee
amortization, true monthly cost, total cost of ownership, income-multiple
qualification, renewal-vs-relocate scenarios, and rent-control-aware
year-2 projections - is computed here, not inside an LLM prompt. Same
inputs.json always produces byte-identical results.json, so the numbers on
the dashboard are reproducible and regression-testable run to run.

Skills and the dashboard read results.json and narrate it in prose. They
never recompute a dollar figure themselves. See references/costs.md for
the declared assumption stack and the rent-control resolution procedure
this module assumes has already run.

Library usage (pure functions, no I/O, no network):

    from costs import true_monthly_cost, tco, qualification, year2_gross

CLI usage:

    python3 costs.py inputs.json > results.json

    # Optional local income comparison (never written to inputs.json,
    # results.json, or any other committed file - run-time only):
    python3 costs.py inputs.json --income 180000 > results.json

inputs.json shape:

    {
      "assumptions": {                    # optional, overrides DEFAULT_ASSUMPTIONS
        "lease_term_months": 12,
        "renewal_increase_pct": 3.0,
        "deposit_float_rate_pct": 5.0,
        "qualification_multiple": 40
      },
      "current_lease": {                  # optional - omit entirely if the
                                           # user has no current lease configured
        "gross": 4200,
        "assumed_increase_pct": null,     # optional; falls back to the
                                           # assumption stack default
        "rent_control": null              # optional {status, cap_pct, source}
      },
      "units": {
        "<unit_id>": {
          "gross": 4800,
          "free_months": 1,
          "term_months": 12,
          "one_time_fees": {"application": 75, "admin": 250, "broker_fee": 4800},
          "recurring_fees": {"amenity": 50, "pet_rent": 75},
          "commute_fare_monthly": 132.0,
          "rent_control": {"status": "controlled", "cap_pct": 3.0,
                            "source": "https://... cited ordinance section"},
          "moving_costs": 1800,           # optional - enables a
                                           # renewal_vs_relocate block with
                                           # this unit as the relocate target
          "new_one_time_fees": {}         # optional - defaults to
                                           # one_time_fees when omitted
        }
      }
    }

results.json embeds an "assumptions" block (every rate and term the run
used) so an LLM narrating it never has to guess what was assumed, plus an
"engine_version". There is no timestamp field: identical input always
produces an identical result.

Notes learned the hard way:
- A fee dict or fare that is absent from a unit's input is not a guessed
  zero. It is recorded in that breakdown's "notes" and flips "complete" to
  False, even though the arithmetic treats the missing category as 0 (there
  is nothing on file to add). Render "n/a - not listed", not a bare "$0".
- Rent-control status is resolved by the research stage from public
  ordinance and tax records and handed to this module as a plain
  {status, cap_pct, source} dict. This module never infers, guesses, or
  defaults a status - UNKNOWN always falls back to the declared
  assumed-increase rate and says so in the returned "basis" string.
- Renewal increases and amortization horizons are declared assumptions,
  never market predictions. Every figure in a result traces to a listed
  fee, a cited rent-control cap, or one of these declared assumptions.
- income_annual is a run-time-only figure, never a field of inputs.json.
  build_results() takes it as a separate keyword argument and the CLI
  takes it as a --income flag; validate_inputs() raises loudly if a
  caller puts "income_annual" inside the inputs.json-shaped dict, because
  that dict is what gets written to disk and income must never land
  there. qualification() itself never returns the income figure or a
  ratio that could reconstruct it (income_annual / gross) - only
  required_income (derived from rent alone) and a qualifies boolean.
- A unit that omits term_months is not a partial input: validate_inputs()
  accepts it and defaults it from assumptions["lease_term_months"], so
  build_results() must resolve that same default once and thread it into
  every function that reads unit_inputs["term_months"] off the dict
  directly (tco(), and rank_by_tco24() by way of the unit dict it is
  handed) - not just into the functions that take term_months as an
  explicit parameter.
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

ENGINE_VERSION = "1.0.0"

# The uniform assumption stack. Every unit in a run is compared on the same
# stack - per-unit ad-hoc assumptions are not allowed, or rankings would
# stop comparing like with like. Override via inputs.json's "assumptions"
# block (which the config.yml -> inputs.json builder step populates from
# the user's own overrides upstream of this module).
DEFAULT_ASSUMPTIONS: dict[str, float] = {
    "lease_term_months": 12,
    "renewal_increase_pct": 3.0,
    "deposit_float_rate_pct": 5.0,
    "qualification_multiple": 40,
}

_KNOWN_RENT_CONTROL_LABELS = ("UNKNOWN", "exempt")


def _money(value: float) -> float:
    """Round a dollar figure to cents. Centralized so every function
    rounds identically and waterfall line items reconcile with totals."""
    return round(float(value), 2)


# ---------------------------------------------------------------------
# Core money math
# ---------------------------------------------------------------------


def net_effective_rent(gross: float, free_months: float, term_months: int) -> float:
    """Amortize a free-rent concession evenly across the lease term.

    One month free on a 12-month lease reduces every month's effective
    rent by 1/12 of gross: the tenant pays gross for
    (term_months - free_months) months and nothing for free_months, and
    that total is spread evenly across the full term.
    """
    if term_months <= 0:
        raise ValueError("term_months must be positive")
    if free_months < 0:
        raise ValueError("free_months cannot be negative")
    if free_months > term_months:
        raise ValueError("free_months cannot exceed term_months")
    total_paid = gross * (term_months - free_months)
    return _money(total_paid / term_months)


def amortized_one_time(fees: dict[str, float], term_months: int) -> float:
    """Spread one-time fees (application, admin, broker, deposit float,
    ...) evenly across term_months. A missing or empty fee dict yields
    0.0 - a confirmed absence of fees, never a guess."""
    if term_months <= 0:
        raise ValueError("term_months must be positive")
    total = sum(float(v) for v in (fees or {}).values())
    return _money(total / term_months)


def deposit_float_fee(deposit: float, annual_rate_pct: float, term_months: int) -> float:
    """Opportunity-cost fee for a security deposit tied up for the lease
    term, as a one-time dollar figure suitable for inclusion in a
    one_time_fees dict passed to amortized_one_time / true_monthly_cost."""
    if term_months <= 0:
        raise ValueError("term_months must be positive")
    years = term_months / 12.0
    return _money(deposit * (annual_rate_pct / 100.0) * years)


def true_monthly_cost(unit_inputs: dict[str, Any], term_months: int) -> dict[str, Any]:
    """Assemble the full monthly cost picture for one unit over one
    lease-length view.

    unit_inputs: {
      "gross": float                       (required)
      "free_months": float                 (optional, default 0)
      "term_months": int                   (documented for callers such as
                                             tco() / rank_by_tco24() that
                                             read the lease length directly
                                             off the dict; this function
                                             itself uses the term_months
                                             parameter below for the math)
      "one_time_fees": dict[str, float]     (optional; None/absent = "not
                                              listed", {} = "confirmed none")
      "recurring_fees": dict[str, float]    (optional; same convention)
      "commute_fare_monthly": float | None  (optional; None = "not known")
    }
    term_months: the amortization horizon for both the free-rent
        concession and the one-time fees. Pass unit_inputs["term_months"]
        for the standard same-lease view.

    Returns {gross, net_effective, one_time_monthly, recurring_monthly,
    commute_monthly, total, waterfall, complete, notes}. waterfall is a
    list of {"label", "monthly", "source"} line items whose monthly
    values sum exactly to total; "source" traces each line to a listing
    field, transit.json, or "not listed" / "MISSING" when the run had
    nothing on file for that line.
    """
    if term_months <= 0:
        raise ValueError("term_months must be positive")
    gross = unit_inputs.get("gross")
    if gross is None:
        raise ValueError("unit_inputs['gross'] is required")
    free_months = unit_inputs.get("free_months")
    if free_months is None:
        free_months = 0

    one_time_fees = unit_inputs.get("one_time_fees")
    recurring_fees = unit_inputs.get("recurring_fees")
    commute = unit_inputs.get("commute_fare_monthly")

    net_eff = net_effective_rent(gross, free_months, term_months)
    one_time_monthly = amortized_one_time(one_time_fees or {}, term_months)
    recurring_monthly = _money(sum(float(v) for v in (recurring_fees or {}).values()))
    commute_monthly = _money(commute) if commute is not None else 0.0

    notes: list[str] = []
    complete = True
    if one_time_fees is None:
        notes.append("one_time_fees: not listed")
        complete = False
    if recurring_fees is None:
        notes.append("recurring_fees: not listed")
        complete = False
    if commute is None:
        notes.append("commute_fare_monthly: MISSING - excludes commute")
        complete = False

    total = _money(net_eff + one_time_monthly + recurring_monthly + commute_monthly)

    waterfall = [
        {"label": "gross_rent", "monthly": _money(gross), "source": "listing field: gross"},
        {
            "label": "concession_credit",
            "monthly": _money(net_eff - gross),
            "source": (
                "listing field: free_months"
                if unit_inputs.get("free_months") is not None
                else "not listed"
            ),
        },
        {
            "label": "one_time_fees_amortized",
            "monthly": one_time_monthly,
            "source": "listing field: one_time_fees" if one_time_fees is not None else "not listed",
        },
        {
            "label": "recurring_fees",
            "monthly": recurring_monthly,
            "source": "listing field: recurring_fees" if recurring_fees is not None else "not listed",
        },
        {
            "label": "commute",
            "monthly": commute_monthly,
            "source": "transit.json: commute_fare_monthly" if commute is not None else "MISSING",
        },
    ]

    return {
        "gross": _money(gross),
        "net_effective": net_eff,
        "one_time_monthly": one_time_monthly,
        "recurring_monthly": recurring_monthly,
        "commute_monthly": commute_monthly,
        "total": total,
        "waterfall": waterfall,
        "complete": complete,
        "notes": notes,
    }


def tco(
    unit_inputs: dict[str, Any], months: int, year2: dict[str, Any] | None = None
) -> float:
    """Total cost of occupying a unit over a 12- or 24-month horizon.

    months=12 is simply true_monthly_cost's total, amortized over the
    unit's own term_months, times 12.

    months=24 depends on whether unit_inputs["term_months"] is itself a
    24-month lease or a 12-month lease being projected through a renewal:

    - term_months == 24: a single continuous lease spans the whole
      horizon. There is no renewal event to reprice, so true_monthly_cost
      already amortizes the concession and one-time fees evenly across
      all 24 months, and the total is simply that monthly total times 24.
      Any `year2` argument is accepted but ignored - there is nothing to
      reprice within a single 24-month lease.
    - term_months == 12: the horizon splits into two 12-month blocks.
      Year 1 is the true_monthly_cost view for the 12-month lease
      (concessions apply, one-time fees are amortized and fully paid
      within the lease term). Year 2 assumes the concession has expired
      and the one-time fees are already paid, and reprices gross rent via
      `year2`:

      year2: optional {"gross2": float}. If omitted, year-2 gross
          defaults to the unit's own gross (concessions expired, no
          assumed increase). Callers wanting a rent-control-aware or
          assumption-driven year-2 figure should call year2_gross()
          first and pass its "year2" value here - rank_by_tco24() does
          exactly that.

      Recurring fees and the commute fare are assumed unchanged in year 2
      (no data source states otherwise).
    - Any other term_months (for example 6 or 18) has no declared
      renewal-projection or single-lease model in this engine and raises
      ValueError rather than silently mixing the two blocks at the wrong
      cadence - a loud failure instead of a quietly wrong dollar figure.
    """
    if months not in (12, 24):
        raise ValueError("months must be 12 or 24")
    lease_term = unit_inputs.get("term_months")
    if lease_term is None:
        raise ValueError("unit_inputs['term_months'] is required")

    year1 = true_monthly_cost(unit_inputs, lease_term)
    year1_total = _money(year1["total"] * 12)
    if months == 12:
        return year1_total

    if lease_term == 24:
        # Single continuous 24-month lease: months 13-24 use the exact
        # same amortized monthly rate as months 1-12 (true_monthly_cost
        # already smoothed the concession and one-time fees over the full
        # term), so the 24-month total is that rate times 24 - not two
        # differently-priced 12-month blocks.
        return _money(year1["total"] * 24)

    if lease_term != 12:
        raise ValueError(
            "tco(months=24) only supports unit_inputs['term_months'] of 12 "
            "(year 2 reprices via a renewal, see `year2`) or 24 (a single "
            f"lease spans the whole horizon, no renewal to reprice); got {lease_term!r}"
        )

    gross = unit_inputs.get("gross")
    if year2 and year2.get("gross2") is not None:
        gross2 = year2["gross2"]
    else:
        gross2 = gross
    recurring_monthly = _money(
        sum(float(v) for v in (unit_inputs.get("recurring_fees") or {}).values())
    )
    commute = unit_inputs.get("commute_fare_monthly")
    commute_monthly = _money(commute) if commute is not None else 0.0
    year2_monthly_total = _money(gross2 + recurring_monthly + commute_monthly)
    year2_total = _money(year2_monthly_total * 12)
    return _money(year1_total + year2_total)


def qualification(
    income_annual: float | None, gross: float, multiple: float = 40
) -> dict[str, Any]:
    """Income-multiple qualification math.

    required_income is derived from rent alone and needs no personal
    data. income_annual is an optional, run-time-only local input: if
    supplied it yields a qualifies boolean, but the income figure itself
    is never part of this function's return value - and neither is any
    other figure derived from it. In particular this deliberately does
    NOT return an income/gross ratio: a ratio times gross trivially
    reconstructs the income figure it was computed from, which would
    defeat the whole point of never persisting income_annual. A caller
    that only forwards this return dict into a committed file never
    leaks the income figure, not even indirectly.
    """
    if gross <= 0:
        raise ValueError("gross must be positive")
    if multiple <= 0:
        raise ValueError("multiple must be positive")
    required_income = _money(gross * multiple)
    if income_annual is None:
        return {"required_income": required_income, "qualifies": None}
    qualifies = income_annual >= required_income
    return {"required_income": required_income, "qualifies": qualifies}


def year2_gross(
    gross: float, rent_control: dict[str, Any] | None, assumed_increase_pct: float
) -> dict[str, Any]:
    """Resolve the year-2 gross rent, capped where a building is actually
    rent-controlled.

    rent_control: {"status": "controlled"|"exempt"|"UNKNOWN", "cap_pct": float|None,
                    "source": str|None} - resolved once per building by the
                    research stage. None (not supplied at all) is treated
                    identically to status "UNKNOWN".

    "controlled" requires a numeric cap_pct (the research stage must have
    resolved an actual cap, not just applicability) and applies it. Any
    other status - "exempt", "UNKNOWN", None, or an unrecognized string -
    applies assumed_increase_pct instead and says so in "basis": control
    status is never guessed, only ever cited or declared UNKNOWN.
    """
    status = "UNKNOWN"
    if rent_control:
        status = rent_control.get("status") or "UNKNOWN"

    if status == "controlled":
        cap_pct = rent_control.get("cap_pct") if rent_control else None
        if cap_pct is None:
            raise ValueError("rent_control status 'controlled' requires a numeric cap_pct")
        year2 = _money(gross * (1 + cap_pct / 100.0))
        basis = "capped by cited ordinance"
    else:
        label = status if status in _KNOWN_RENT_CONTROL_LABELS else "UNKNOWN"
        year2 = _money(gross * (1 + assumed_increase_pct / 100.0))
        basis = f"assumed increase (control status {label})"

    return {"year2": year2, "basis": basis}


def renewal_vs_relocate(
    current: dict[str, Any],
    move: dict[str, Any],
    assumptions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Side-by-side 24-month cost of renewing the current lease versus
    moving to a candidate unit.

    current: {"gross": float, "assumed_increase_pct": float|None,
              "rent_control": dict|None}
        assumed_increase_pct falls back to the declared assumption stack
        default when omitted; rent_control (if given) can still cap the
        year-2 figure via year2_gross() regardless.
    move: {"new_gross": float, "new_free_months": float,
           "new_term_months": int, "moving_costs": float,
           "new_one_time_fees": dict[str, float]}
        moving_costs is a declared, already-computed lump sum (mover cost
        plus any overlap-rent days) - this function does not derive it.

    Returns {"renew_24mo", "relocate_24mo", "cheaper": "renew"|"relocate",
    "delta", "assumptions_used"}. delta = relocate_24mo - renew_24mo:
    positive means moving costs more (renewing is cheaper); negative means
    moving is cheaper. assumptions_used echoes every rate and figure this
    call actually applied (the resolved renewal increase and its basis,
    moving costs, and the new lease's term) so a caller never has to
    re-derive or guess what was assumed.
    """
    assumptions = {**DEFAULT_ASSUMPTIONS, **(assumptions or {})}

    gross = current.get("gross")
    if gross is None:
        raise ValueError("current['gross'] is required")
    rent_control = current.get("rent_control")
    assumed_increase_pct = current.get("assumed_increase_pct")
    if assumed_increase_pct is None:
        assumed_increase_pct = assumptions["renewal_increase_pct"]

    y2 = year2_gross(gross, rent_control, assumed_increase_pct)
    renew_24mo = _money(gross * 12 + y2["year2"] * 12)

    new_gross = move.get("new_gross")
    if new_gross is None:
        raise ValueError("move['new_gross'] is required")
    new_free_months = move.get("new_free_months", 0)
    new_term_months = move.get("new_term_months") or assumptions["lease_term_months"]
    moving_costs = move.get("moving_costs", 0.0)
    new_one_time_fees = move.get("new_one_time_fees") or {}

    move_unit_inputs = {
        "gross": new_gross,
        "free_months": new_free_months,
        "term_months": new_term_months,
        "one_time_fees": new_one_time_fees,
    }
    year1_move = true_monthly_cost(move_unit_inputs, new_term_months)
    # Year 2 at the new place: concession expired, one-time fees already
    # paid off in year 1, full new_gross carries forward (no increase is
    # declared for a unit the tenant has not yet leased).
    relocate_24mo = _money(moving_costs + year1_move["total"] * 12 + new_gross * 12)

    delta = _money(relocate_24mo - renew_24mo)
    cheaper = "renew" if renew_24mo <= relocate_24mo else "relocate"
    return {
        "renew_24mo": renew_24mo,
        "relocate_24mo": relocate_24mo,
        "cheaper": cheaper,
        "delta": delta,
        "assumptions_used": {
            "renewal_increase_pct": assumed_increase_pct,
            "renewal_increase_basis": y2["basis"],
            "moving_costs": _money(moving_costs),
            "new_term_months": new_term_months,
        },
    }


def rank_by_tco24(
    units: list[dict[str, Any]], assumed_increase_pct: float
) -> list[dict[str, Any]]:
    """Rank candidate units by 24-month total cost of ownership.

    units: [{"unit_id": str, "inputs": unit_inputs, "rent_control": dict|None}]
    assumed_increase_pct: the declared fallback increase applied to any
        unit whose rent_control is missing, exempt, or UNKNOWN.

    Each unit's year-2 gross is resolved via year2_gross() (a controlled
    building's cited cap wins over the fallback assumption). Returns rows
    sorted ascending by tco_24 (cheapest first), tie-broken by unit_id for
    a deterministic order - this is how a unit that is cheaper today can
    still rank behind a rent-controlled sibling once the capped year-2
    rent is priced in.
    """
    rows: list[dict[str, Any]] = []
    for unit in units:
        unit_id = unit["unit_id"]
        inputs = unit["inputs"]
        rent_control = unit.get("rent_control")
        gross = inputs.get("gross")
        if gross is None:
            raise ValueError(f"unit '{unit_id}': inputs['gross'] is required")
        y2 = year2_gross(gross, rent_control, assumed_increase_pct)
        tco_24 = tco(inputs, 24, year2={"gross2": y2["year2"]})
        tco_12 = tco(inputs, 12)
        rows.append(
            {
                "unit_id": unit_id,
                "tco_12": tco_12,
                "tco_24": tco_24,
                "year2_gross": y2["year2"],
                "year2_basis": y2["basis"],
            }
        )
    rows.sort(key=lambda r: (r["tco_24"], r["unit_id"]))
    return rows


# ---------------------------------------------------------------------
# inputs.json -> results.json
# ---------------------------------------------------------------------


def validate_inputs(data: Any) -> None:
    """Raise ValueError with a specific message on structurally malformed
    inputs.json. Deliberately not a full JSON-schema validator - just the
    checks needed so a bad input fails loudly instead of computing
    nonsense or a fabricated number."""
    if not isinstance(data, dict):
        raise ValueError("inputs.json must be a JSON object")
    if "income_annual" in data:
        raise ValueError(
            "inputs.json must not contain 'income_annual' - personal income is a "
            "run-time-only input. Pass it to build_results(data, income_annual=...) "
            "or the CLI's --income flag instead; it must never be written to "
            "inputs.json, results.json, or any other committed file."
        )
    units = data.get("units")
    if not isinstance(units, dict) or not units:
        raise ValueError("inputs.json must contain a non-empty 'units' object")
    assumptions = data.get("assumptions")
    if assumptions is not None and not isinstance(assumptions, dict):
        raise ValueError("inputs.json 'assumptions' must be an object")
    default_term = (assumptions or {}).get(
        "lease_term_months", DEFAULT_ASSUMPTIONS["lease_term_months"]
    )
    for unit_id, unit in units.items():
        if not isinstance(unit, dict):
            raise ValueError(f"unit '{unit_id}' must be an object")
        gross = unit.get("gross")
        if not isinstance(gross, (int, float)) or isinstance(gross, bool):
            raise ValueError(f"unit '{unit_id}' missing required numeric field 'gross'")
        term = unit.get("term_months", default_term)
        if not isinstance(term, (int, float)) or isinstance(term, bool) or term <= 0:
            raise ValueError(f"unit '{unit_id}' has an invalid 'term_months'")


def build_results(
    data: dict[str, Any], income_annual: float | None = None
) -> dict[str, Any]:
    """Pure transform: an inputs.json-shaped dict to a results.json-shaped
    dict. No I/O, no timestamp - identical input always yields an
    identical return value, which is what makes the CLI's stdout
    byte-identical across runs on the same inputs.json.

    income_annual is a separate, run-time-only keyword argument, never a
    field read off `data`. This keeps the on-disk inputs.json file (which
    `data` is a parsed copy of) free of personal income data by
    construction - validate_inputs() also rejects an "income_annual" key
    inside `data` itself so a caller cannot smuggle it in through the
    file. The CLI's --income flag is the run-time channel for this
    argument; nothing else ever writes it to a file.
    """
    validate_inputs(data)
    assumptions = {**DEFAULT_ASSUMPTIONS, **(data.get("assumptions") or {})}
    current_lease = data.get("current_lease")

    units_out: dict[str, Any] = {}
    rank_inputs: list[dict[str, Any]] = []

    for unit_id, unit in sorted(data["units"].items()):
        term_months = unit.get("term_months", assumptions["lease_term_months"])
        # Thread the resolved term_months back into the unit dict used
        # from here on, so every downstream function that reads
        # unit_inputs["term_months"] directly off the dict (tco(), and
        # rank_by_tco24() by way of rank_inputs below) sees the same
        # defaulted value build_results just resolved - never re-derives
        # it, and never crashes on a unit that legitimately omitted its
        # own term_months.
        if unit.get("term_months") != term_months:
            unit = {**unit, "term_months": term_months}
        tmc12 = true_monthly_cost(unit, term_months)
        rent_control = unit.get("rent_control")
        y2 = year2_gross(unit["gross"], rent_control, assumptions["renewal_increase_pct"])
        tco_24 = tco(unit, 24, year2={"gross2": y2["year2"]})

        unit_result: dict[str, Any] = {
            "true_monthly_cost_12": tmc12,
            "true_monthly_cost_24": {
                "tco_24": tco_24,
                "year2_gross": y2["year2"],
                "year2_basis": y2["basis"],
            },
            "qualification": qualification(
                income_annual, unit["gross"], assumptions["qualification_multiple"]
            ),
        }

        if current_lease:
            move = {
                "new_gross": unit["gross"],
                "new_free_months": unit.get("free_months", 0),
                "new_term_months": term_months,
                "moving_costs": unit.get("moving_costs", 0.0),
                "new_one_time_fees": unit.get("new_one_time_fees", unit.get("one_time_fees"))
                or {},
            }
            unit_result["renewal_vs_relocate"] = renewal_vs_relocate(
                current_lease, move, assumptions
            )

        units_out[unit_id] = unit_result
        rank_inputs.append({"unit_id": unit_id, "inputs": unit, "rent_control": rent_control})

    result: dict[str, Any] = {
        "engine_version": ENGINE_VERSION,
        "assumptions": assumptions,
        "units": units_out,
    }
    if len(rank_inputs) >= 2:
        result["ranking"] = {
            "by_tco_24": rank_by_tco24(rank_inputs, assumptions["renewal_increase_pct"])
        }
    return result


def main() -> int:
    """CLI entry point.

    --income is the only channel for a run-time income comparison: it is
    read into a local variable here and passed straight to build_results()
    as a keyword argument, never written into inputs.json, never part of
    `args` once parsed out, and never touched again after this call
    returns. See qualification() and build_results() for why it never
    reaches results.json either.
    """
    args = sys.argv[1:]
    usage = "usage: costs.py inputs.json [--income ANNUAL_INCOME]"
    income_annual: float | None = None
    if "--income" in args:
        idx = args.index("--income")
        if idx + 1 >= len(args):
            print(usage, file=sys.stderr)
            return 2
        try:
            income_annual = float(args[idx + 1])
        except ValueError:
            print(usage, file=sys.stderr)
            return 2
        args = args[:idx] + args[idx + 2 :]

    if len(args) != 1:
        print(usage, file=sys.stderr)
        return 2
    src = pathlib.Path(args[0])
    try:
        data = json.loads(src.read_text())
        result = build_results(data, income_annual=income_annual)
    except FileNotFoundError:
        print(f"error: no such file: {src}", file=sys.stderr)
        return 1
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
