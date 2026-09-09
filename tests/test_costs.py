"""Golden-file tests for the deterministic cost engine (costs.py).

Every dollar figure asserted here was hand-computed in the test itself
(see the inline arithmetic in each test's comments) and cross-checked
against the implementation before being pinned as a golden value - not
merely re-derived from the code under test.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import costs

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / ".claude" / "skills" / "apartmentops" / "scripts" / "costs.py"
)


# ---------------------------------------------------------------------
# net_effective_rent
# ---------------------------------------------------------------------


def test_net_effective_rent_one_month_free():
    # 4800 * (12 - 1) / 12 = 4800 * 11 / 12 = 4400.0
    assert costs.net_effective_rent(4800, 1, 12) == 4400.0


def test_net_effective_rent_no_concession():
    assert costs.net_effective_rent(4800, 0, 12) == 4800.0


def test_net_effective_rent_full_term_free_is_zero():
    assert costs.net_effective_rent(4800, 12, 12) == 0.0


def test_net_effective_rent_rejects_free_months_over_term():
    with pytest.raises(ValueError):
        costs.net_effective_rent(4800, 13, 12)


def test_net_effective_rent_rejects_negative_free_months():
    with pytest.raises(ValueError):
        costs.net_effective_rent(4800, -1, 12)


def test_net_effective_rent_rejects_nonpositive_term():
    with pytest.raises(ValueError):
        costs.net_effective_rent(4800, 0, 0)


# ---------------------------------------------------------------------
# amortized_one_time
# ---------------------------------------------------------------------


def test_amortized_one_time_basic():
    # (75 + 250 + 4800) / 12 = 5125 / 12 = 427.0833... -> 427.08
    fees = {"application": 75, "admin": 250, "broker_fee": 4800}
    assert costs.amortized_one_time(fees, 12) == 427.08


def test_amortized_one_time_empty_dict_is_zero():
    assert costs.amortized_one_time({}, 12) == 0.0


def test_amortized_one_time_none_is_zero():
    assert costs.amortized_one_time(None, 12) == 0.0  # type: ignore[arg-type]


def test_amortized_one_time_rejects_nonpositive_term():
    with pytest.raises(ValueError):
        costs.amortized_one_time({"application": 75}, 0)


def test_deposit_float_fee():
    # $3000 deposit, 5%/yr, 12-month term -> 3000 * 0.05 * 1.0 = 150.0
    assert costs.deposit_float_fee(3000, 5.0, 12) == 150.0
    # 24-month term doubles the float years -> 300.0
    assert costs.deposit_float_fee(3000, 5.0, 24) == 300.0


# ---------------------------------------------------------------------
# true_monthly_cost
# ---------------------------------------------------------------------


def _full_unit() -> dict:
    return {
        "gross": 4800,
        "free_months": 1,
        "term_months": 12,
        "one_time_fees": {"broker_fee": 4800},
        "recurring_fees": {"pet_rent": 75},
        "commute_fare_monthly": 132.50,
    }


def test_true_monthly_cost_complete_unit_matches_hand_computed_total():
    # net_effective = 4800 - 4800/12 = 4400.0
    # one_time_monthly = 4800 / 12 = 400.0
    # recurring_monthly = 75.0
    # commute_monthly = 132.50
    # total = 4400 + 400 + 75 + 132.50 = 5007.50
    result = costs.true_monthly_cost(_full_unit(), 12)
    assert result["gross"] == 4800.0
    assert result["net_effective"] == 4400.0
    assert result["one_time_monthly"] == 400.0
    assert result["recurring_monthly"] == 75.0
    assert result["commute_monthly"] == 132.50
    assert result["total"] == 5007.50
    assert result["complete"] is True
    assert result["notes"] == []


def test_true_monthly_cost_waterfall_sums_to_total():
    result = costs.true_monthly_cost(_full_unit(), 12)
    waterfall_sum = sum(line["monthly"] for line in result["waterfall"])
    assert waterfall_sum == pytest.approx(result["total"])
    labels = [line["label"] for line in result["waterfall"]]
    assert labels == [
        "gross_rent",
        "concession_credit",
        "one_time_fees_amortized",
        "recurring_fees",
        "commute",
    ]
    # every line traces to a source - a listing field, transit.json, or an
    # explicit "not listed" / "MISSING" when nothing was on file
    for line in result["waterfall"]:
        assert line["source"]


def test_true_monthly_cost_waterfall_flags_unsourced_lines_as_not_listed_or_missing():
    unit = {"gross": 4600, "term_months": 12}  # no fee keys, no commute
    result = costs.true_monthly_cost(unit, 12)
    by_label = {line["label"]: line["source"] for line in result["waterfall"]}
    assert by_label["one_time_fees_amortized"] == "not listed"
    assert by_label["recurring_fees"] == "not listed"
    assert by_label["commute"] == "MISSING"


def test_true_monthly_cost_missing_fee_data_flags_incomplete_not_a_guess():
    unit = {"gross": 4600, "term_months": 12}  # no fee keys, no commute at all
    result = costs.true_monthly_cost(unit, 12)
    assert result["one_time_monthly"] == 0.0
    assert result["recurring_monthly"] == 0.0
    assert result["commute_monthly"] == 0.0
    assert result["complete"] is False
    assert "one_time_fees: not listed" in result["notes"]
    assert "recurring_fees: not listed" in result["notes"]
    assert any("commute_fare_monthly" in n for n in result["notes"])
    # the unit still totals correctly even with nothing on file for fees
    assert result["total"] == 4600.0


def test_true_monthly_cost_confirmed_empty_fees_is_complete():
    # Explicit {} means "checked, none apply" - distinct from omission.
    unit = {
        "gross": 4600,
        "term_months": 12,
        "one_time_fees": {},
        "recurring_fees": {},
        "commute_fare_monthly": 0,
    }
    result = costs.true_monthly_cost(unit, 12)
    assert result["complete"] is True
    assert result["notes"] == []


def test_true_monthly_cost_missing_commute_excludes_it_without_failing():
    unit = {
        "gross": 4600,
        "term_months": 12,
        "one_time_fees": {"application": 75},
        "recurring_fees": {},
    }
    result = costs.true_monthly_cost(unit, 12)
    assert result["commute_monthly"] == 0.0
    assert result["complete"] is False
    assert any("excludes commute" in n for n in result["notes"])
    # run does not fail; total is still a real number
    assert result["total"] == 4606.25  # 4600 + 75/12 (6.25)


def test_true_monthly_cost_explicit_null_free_months_treated_as_zero():
    unit = {"gross": 4600, "free_months": None, "term_months": 12}
    result = costs.true_monthly_cost(unit, 12)
    assert result["net_effective"] == 4600.0


def test_true_monthly_cost_requires_gross():
    with pytest.raises(ValueError):
        costs.true_monthly_cost({"term_months": 12}, 12)


# ---------------------------------------------------------------------
# tco (12 and 24 month)
# ---------------------------------------------------------------------


def test_tco_12_is_true_monthly_cost_times_twelve():
    unit = _full_unit()
    assert costs.tco(unit, 12) == pytest.approx(5007.50 * 12)


def test_tco_24_reflects_concession_expiry_and_renewal_reversion():
    unit = _full_unit()
    y2 = costs.year2_gross(4800, None, 3.0)
    assert y2 == {"year2": 4944.0, "basis": "assumed increase (control status UNKNOWN)"}
    tco24 = costs.tco(unit, 24, year2={"gross2": y2["year2"]})
    # year1_total = 5007.50 * 12 = 60090.00
    # year2_monthly = 4944 (gross2, concession gone) + 75 (recurring) + 132.50 (commute) = 5151.50
    # year2_total = 5151.50 * 12 = 61818.00
    # tco24 = 60090.00 + 61818.00 = 121908.00
    assert tco24 == 121908.00


def test_tco_24_without_year2_override_defaults_to_flat_gross():
    unit = {"gross": 4500, "free_months": 0, "term_months": 12}
    # No year2 given: year 2 gross defaults to the unit's own gross (no
    # assumed increase applied) - explicitly documented, not a guess of a
    # nonzero increase.
    assert costs.tco(unit, 24) == pytest.approx(4500 * 12 * 2)


def test_tco_rejects_invalid_horizon():
    with pytest.raises(ValueError):
        costs.tco({"gross": 4500, "term_months": 12}, 6)


def test_tco_requires_term_months():
    with pytest.raises(ValueError):
        costs.tco({"gross": 4500}, 12)


def test_tco_24_single_24month_lease_has_no_renewal_reversion():
    # A single continuous 24-month lease: 2 months free and a $4800
    # broker fee amortize evenly across all 24 months (no separate
    # renewal event at month 13). Hand-computed: the tenant pays gross
    # for 22 of the 24 months (4800 * 22 = 105600) plus the one-time
    # broker fee paid once (4800), total 110400 - not the two-block
    # renewal-reversion figure (112800) that treating this like a
    # 12-month lease renewing into year 2 would wrongly produce.
    unit = {
        "gross": 4800,
        "free_months": 2,
        "term_months": 24,
        "one_time_fees": {"broker_fee": 4800},
    }
    assert costs.tco(unit, 24) == 110400.00


def test_tco_24_single_24month_lease_ignores_year2_override():
    # There is no renewal event within a single 24-month lease, so a
    # year2 override (e.g. from a rent-control projection) must not
    # silently change the total.
    unit = {"gross": 4800, "free_months": 2, "term_months": 24}
    with_override = costs.tco(unit, 24, year2={"gross2": 9999})
    without_override = costs.tco(unit, 24)
    assert with_override == without_override


def test_tco_24_rejects_unsupported_term_months():
    # Only term_months == 12 (renews into year 2) or term_months == 24
    # (a single lease spans the whole horizon) are supported at a
    # 24-month horizon. Anything else must fail loudly rather than
    # silently mix the two block models at the wrong cadence.
    unit = {"gross": 4800, "term_months": 18}
    with pytest.raises(ValueError):
        costs.tco(unit, 24)


# ---------------------------------------------------------------------
# qualification (40x boundary)
# ---------------------------------------------------------------------


def test_qualification_required_income_at_default_multiple():
    result = costs.qualification(None, 4800)
    assert result == {"required_income": 192000.0, "qualifies": None}


def test_qualification_boundary_exactly_at_multiple_qualifies():
    result = costs.qualification(192000, 4800, 40)
    assert result["qualifies"] is True


def test_qualification_boundary_one_cent_under_fails():
    result = costs.qualification(191999.99, 4800, 40)
    assert result["qualifies"] is False


def test_qualification_never_echoes_income_in_output():
    result = costs.qualification(275000, 4800, 40)
    assert "income_annual" not in result
    assert 275000 not in result.values()


def test_qualification_never_returns_a_ratio_that_reconstructs_income():
    # A ratio field (income_annual / gross) would let a caller trivially
    # recover the income figure this function is designed to omit -
    # 187500 / 4800 = 39.0625, rounds to 39.06, and 39.06 * 4800 =
    # 187488.0 recovers the income to within a rounding error. The
    # engine must never return that ratio at all.
    result = costs.qualification(187500, 4800, 40)
    assert "ratio" not in result
    assert set(result.keys()) == {"required_income", "qualifies"}


def test_qualification_rejects_nonpositive_gross():
    with pytest.raises(ValueError):
        costs.qualification(100000, 0, 40)


# ---------------------------------------------------------------------
# year2_gross (rent-control resolution)
# ---------------------------------------------------------------------


def test_year2_gross_controlled_uses_cited_cap():
    result = costs.year2_gross(4200, {"status": "controlled", "cap_pct": 3.0, "source": "x"}, 5.0)
    assert result == {"year2": 4326.0, "basis": "capped by cited ordinance"}


def test_year2_gross_exempt_uses_assumed_increase_and_says_so():
    result = costs.year2_gross(4200, {"status": "exempt"}, 5.0)
    assert result["year2"] == 4410.0
    assert result["basis"] == "assumed increase (control status exempt)"


def test_year2_gross_unknown_uses_assumed_increase_and_says_so():
    result = costs.year2_gross(4200, {"status": "UNKNOWN"}, 5.0)
    assert result["basis"] == "assumed increase (control status UNKNOWN)"


def test_year2_gross_missing_rent_control_dict_treated_as_unknown():
    result = costs.year2_gross(4200, None, 5.0)
    assert result["year2"] == 4410.0
    assert result["basis"] == "assumed increase (control status UNKNOWN)"


def test_year2_gross_controlled_without_cap_pct_raises_never_guesses():
    with pytest.raises(ValueError):
        costs.year2_gross(4200, {"status": "controlled"}, 5.0)


# ---------------------------------------------------------------------
# renewal_vs_relocate (flip both directions)
# ---------------------------------------------------------------------


def test_renewal_vs_relocate_renew_wins():
    current = {"gross": 4200, "assumed_increase_pct": 3.0, "rent_control": None}
    move = {
        "new_gross": 4800,
        "new_free_months": 0,
        "new_term_months": 12,
        "moving_costs": 3000,
        "new_one_time_fees": {"broker_fee": 4800, "application": 75},
    }
    result = costs.renewal_vs_relocate(current, move)
    # renew_24mo = 4200*12 + (4200*1.03)*12 = 50400 + 51912 = 102312.00
    assert result["renew_24mo"] == 102312.00
    # relocate: year1 move total = 4800 + (4800+75)/12 = 4800 + 406.25 = 5206.25
    # relocate_24mo = 3000 + 5206.25*12 + 4800*12 = 3000 + 62475 + 57600 = 123075.00
    assert result["relocate_24mo"] == 123075.00
    assert result["cheaper"] == "renew"
    assert result["delta"] == pytest.approx(20763.00)
    assert result["delta"] > 0  # relocating costs more


def test_renewal_vs_relocate_relocate_wins():
    current = {"gross": 5200, "assumed_increase_pct": 5.0, "rent_control": None}
    move = {
        "new_gross": 4200,
        "new_free_months": 1,
        "new_term_months": 12,
        "moving_costs": 800,
        "new_one_time_fees": {"application": 75},
    }
    result = costs.renewal_vs_relocate(current, move)
    # renew_24mo = 5200*12 + (5200*1.05)*12 = 62400 + 65520 = 127920.00
    assert result["renew_24mo"] == 127920.00
    # relocate: net_eff = 4200 - 4200/12 = 3850; one_time = 75/12 = 6.25
    #   year1 move total = 3850 + 6.25 = 3856.25
    #   relocate_24mo = 800 + 3856.25*12 + 4200*12 = 800 + 46275 + 50400 = 97475.00
    assert result["relocate_24mo"] == 97475.00
    assert result["cheaper"] == "relocate"
    assert result["delta"] == pytest.approx(-30445.00)
    assert result["delta"] < 0  # relocating is cheaper


def test_renewal_vs_relocate_echoes_assumptions_used():
    current = {"gross": 4200, "assumed_increase_pct": 3.0, "rent_control": None}
    move = {
        "new_gross": 4800,
        "new_free_months": 0,
        "new_term_months": 12,
        "moving_costs": 3000,
        "new_one_time_fees": {"application": 75},
    }
    result = costs.renewal_vs_relocate(current, move)
    assert result["assumptions_used"] == {
        "renewal_increase_pct": 3.0,
        "renewal_increase_basis": "assumed increase (control status UNKNOWN)",
        "moving_costs": 3000.0,
        "new_term_months": 12,
    }


def test_renewal_vs_relocate_falls_back_to_default_increase_when_unset():
    current = {"gross": 4200, "rent_control": None}  # no assumed_increase_pct given
    move = {"new_gross": 4200, "moving_costs": 0}
    result = costs.renewal_vs_relocate(current, move)
    assert result["assumptions_used"]["renewal_increase_pct"] == costs.DEFAULT_ASSUMPTIONS[
        "renewal_increase_pct"
    ]


def test_renewal_vs_relocate_rent_controlled_current_caps_the_stay_side():
    current = {
        "gross": 4200,
        "assumed_increase_pct": 8.0,  # would be used if not controlled
        "rent_control": {"status": "controlled", "cap_pct": 2.0, "source": "x"},
    }
    move = {"new_gross": 4200, "new_free_months": 0, "new_term_months": 12, "moving_costs": 0}
    result = costs.renewal_vs_relocate(current, move)
    # renew_24mo = 4200*12 + (4200*1.02)*12 = 50400 + 51408 = 101808.00
    assert result["renew_24mo"] == 101808.00


def test_renewal_vs_relocate_requires_current_gross():
    with pytest.raises(ValueError):
        costs.renewal_vs_relocate({}, {"new_gross": 4200})


def test_renewal_vs_relocate_requires_move_new_gross():
    with pytest.raises(ValueError):
        costs.renewal_vs_relocate({"gross": 4200}, {})


# ---------------------------------------------------------------------
# rank_by_tco24 - the month-13 rent-control crossover
# ---------------------------------------------------------------------


def test_rank_by_tco24_month13_rent_control_crossover():
    """Unit A is cheaper today and stays cheaper through month 12, but
    Unit B's cited rent-control cap makes B's year-2 rent (months 13-24)
    rise far less than A's assumed-increase year-2 rent, so B overtakes A
    on 24-month total cost of ownership."""
    unit_a = {"gross": 4500, "free_months": 0, "term_months": 12}
    unit_b = {"gross": 4600, "free_months": 0, "term_months": 12}
    units = [
        {"unit_id": "unit-a", "inputs": unit_a, "rent_control": None},
        {
            "unit_id": "unit-b",
            "inputs": unit_b,
            "rent_control": {
                "status": "controlled",
                "cap_pct": 2.0,
                "source": "https://example.gov/ordinance#sec-3",
            },
        },
    ]

    # Through month 12, A is unambiguously cheaper (lower gross, no fees).
    assert costs.tco(unit_a, 12) == 54000.00
    assert costs.tco(unit_b, 12) == 55200.00
    assert costs.tco(unit_a, 12) < costs.tco(unit_b, 12)

    # A gets the 10% fallback increase (no rent control on file); B gets
    # its cited 2% cap.
    ranked = costs.rank_by_tco24(units, assumed_increase_pct=10.0)

    # unit-a: tco_24 = 54000 + (4500*1.10)*12 = 54000 + 59400 = 113400.00
    # unit-b: tco_24 = 55200 + (4600*1.02)*12 = 55200 + 56304 = 111504.00
    by_id = {row["unit_id"]: row for row in ranked}
    assert by_id["unit-a"]["tco_24"] == 113400.00
    assert by_id["unit-b"]["tco_24"] == 111504.00

    # The cheaper-today unit loses the 24-month ranking to its
    # rent-controlled sibling.
    assert ranked[0]["unit_id"] == "unit-b"
    assert ranked[1]["unit_id"] == "unit-a"
    assert by_id["unit-b"]["year2_basis"] == "capped by cited ordinance"
    assert by_id["unit-a"]["year2_basis"] == "assumed increase (control status UNKNOWN)"


def test_rank_by_tco24_sorted_ascending_cheapest_first():
    units = [
        {"unit_id": "z", "inputs": {"gross": 5000, "term_months": 12}, "rent_control": None},
        {"unit_id": "a", "inputs": {"gross": 4000, "term_months": 12}, "rent_control": None},
    ]
    ranked = costs.rank_by_tco24(units, 3.0)
    assert [row["unit_id"] for row in ranked] == ["a", "z"]


def test_rank_by_tco24_requires_gross_per_unit():
    with pytest.raises(ValueError):
        costs.rank_by_tco24(
            [{"unit_id": "x", "inputs": {"term_months": 12}, "rent_control": None}], 3.0
        )


# ---------------------------------------------------------------------
# build_results / CLI harness
# ---------------------------------------------------------------------


def _sample_inputs() -> dict:
    return {
        "units": {
            "tower2-2207": _full_unit(),
            "tower2-2208": {"gross": 4600, "term_months": 12},
        },
    }


def test_build_results_is_pure_and_deterministic():
    data = _sample_inputs()
    r1 = costs.build_results(data)
    r2 = costs.build_results(data)
    assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)


def test_build_results_no_timestamp_field():
    result = costs.build_results(_sample_inputs())
    blob = json.dumps(result)
    assert "timestamp" not in blob.lower()


def test_build_results_rejects_income_annual_inside_inputs_data():
    # income_annual must never be a field of the inputs.json-shaped dict
    # itself - that dict is what a build step would write to disk. It
    # only ever reaches the engine as build_results()'s separate keyword
    # argument (or the CLI's --income flag).
    data = _sample_inputs()
    data["income_annual"] = 200000
    with pytest.raises(ValueError, match="income_annual"):
        costs.build_results(data)


def test_build_results_income_annual_is_a_runtime_keyword_never_echoed():
    result = costs.build_results(_sample_inputs(), income_annual=200000)
    blob = json.dumps(result)
    assert "200000" not in blob
    assert "income_annual" not in blob
    assert "ratio" not in blob
    # the boolean derived from the run-time income is still present
    qualification = result["units"]["tower2-2207"]["qualification"]
    assert qualification["qualifies"] in (True, False)
    assert set(qualification.keys()) == {"required_income", "qualifies"}


def test_build_results_without_income_leaves_qualifies_none():
    result = costs.build_results(_sample_inputs())
    qualification = result["units"]["tower2-2207"]["qualification"]
    assert qualification["qualifies"] is None


def test_build_results_engine_version_and_assumptions_present():
    result = costs.build_results(_sample_inputs())
    assert result["engine_version"] == costs.ENGINE_VERSION
    assert result["assumptions"] == costs.DEFAULT_ASSUMPTIONS


def test_build_results_complete_unit_and_missing_fee_unit_side_by_side():
    result = costs.build_results(_sample_inputs())
    complete = result["units"]["tower2-2207"]["true_monthly_cost_12"]
    incomplete = result["units"]["tower2-2208"]["true_monthly_cost_12"]
    assert complete["complete"] is True
    assert incomplete["complete"] is False


def test_build_results_ranking_present_for_multiple_units():
    result = costs.build_results(_sample_inputs())
    assert "ranking" in result
    ids = [row["unit_id"] for row in result["ranking"]["by_tco_24"]]
    assert set(ids) == {"tower2-2207", "tower2-2208"}


def test_build_results_no_ranking_for_single_unit():
    data = {"units": {"only-one": {"gross": 4500, "term_months": 12}}}
    result = costs.build_results(data)
    assert "ranking" not in result


def test_build_results_missing_units_raises():
    with pytest.raises(ValueError):
        costs.build_results({})


def test_build_results_unit_missing_gross_raises():
    with pytest.raises(ValueError):
        costs.build_results({"units": {"bad-unit": {"term_months": 12}}})


def test_build_results_malformed_not_a_dict_raises():
    with pytest.raises(ValueError):
        costs.build_results({"units": {"bad-unit": "not-an-object"}})


def test_build_results_current_lease_produces_renewal_scenario():
    data = _sample_inputs()
    data["current_lease"] = {"gross": 4300, "assumed_increase_pct": 3.0, "rent_control": None}
    result = costs.build_results(data)
    scenario = result["units"]["tower2-2207"]["renewal_vs_relocate"]
    assert scenario["cheaper"] in ("renew", "relocate")
    assert isinstance(scenario["renew_24mo"], float)
    assert isinstance(scenario["relocate_24mo"], float)


def test_build_results_omits_renewal_scenario_when_no_current_lease():
    result = costs.build_results(_sample_inputs())
    assert "renewal_vs_relocate" not in result["units"]["tower2-2207"]


def test_build_results_unit_without_term_months_uses_default_and_does_not_crash():
    # validate_inputs() explicitly accepts a unit missing term_months and
    # defaults it from assumptions.lease_term_months (12 by default) -
    # build_results() must actually use that same defaulted value
    # everywhere, including inside tco(), instead of crashing because the
    # raw unit dict (still missing term_months) reaches tco() directly.
    data = {"units": {"u1": {"gross": 4500}}}
    result = costs.build_results(data)
    unit_result = result["units"]["u1"]
    # true_monthly_cost_12 amortized over the defaulted 12-month term:
    # no fees, no concession -> total is just gross.
    assert unit_result["true_monthly_cost_12"]["total"] == 4500.0
    # tco_24 used the same defaulted 12-month term_months (renewal
    # reversion branch), not a crash: year1 = 4500*12 = 54000, year 2
    # reprices at the default 3.0% assumed increase (no rent_control on
    # file) -> 4500*1.03*12 = 55620, tco_24 = 54000 + 55620 = 109620.00.
    assert unit_result["true_monthly_cost_24"]["tco_24"] == 109620.00


def test_build_results_unit_without_term_months_honors_assumption_override():
    # lease_term_months overridden to 24 in assumptions: a unit that
    # still omits its own term_months gets the overridden default
    # threaded all the way through, landing in the single-24-month-lease
    # branch of tco() (no renewal reversion) rather than crashing or
    # silently using the wrong horizon.
    data = {
        "assumptions": {"lease_term_months": 24},
        "units": {"u1": {"gross": 4500, "free_months": 2}},
    }
    result = costs.build_results(data)
    unit_result = result["units"]["u1"]
    # net_effective = 4500 * (24 - 2) / 24 = 4125.0, no fees -> total 4125
    assert unit_result["true_monthly_cost_12"]["total"] == 4125.0
    # tco_24 for a single 24-month lease: 4125 * 24 = 99000.00
    assert unit_result["true_monthly_cost_24"]["tco_24"] == 99000.00


def test_build_results_ranking_threads_defaulted_term_months_too():
    # rank_by_tco24() receives the unit dict build_results() assembled
    # for ranking (via rank_inputs) and calls tco() on it directly -
    # that dict must already carry the defaulted term_months or ranking
    # crashes for any unit that omitted its own term_months.
    data = {
        "units": {
            "no-term": {"gross": 4500},
            "with-term": {"gross": 4600, "term_months": 12},
        }
    }
    result = costs.build_results(data)
    ids = [row["unit_id"] for row in result["ranking"]["by_tco_24"]]
    assert set(ids) == {"no-term", "with-term"}


# ---------------------------------------------------------------------
# CLI: costs.py inputs.json -> stdout, byte-identical across runs
# ---------------------------------------------------------------------


def test_cli_round_trip_byte_identical(tmp_path):
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_text(json.dumps(_sample_inputs()))

    run1 = subprocess.run(
        [sys.executable, str(SCRIPT), str(inputs_path)],
        capture_output=True, text=True, check=True,
    )
    run2 = subprocess.run(
        [sys.executable, str(SCRIPT), str(inputs_path)],
        capture_output=True, text=True, check=True,
    )
    assert run1.stdout == run2.stdout
    parsed = json.loads(run1.stdout)
    assert parsed["engine_version"] == costs.ENGINE_VERSION


def test_cli_income_flag_never_appears_in_output(tmp_path):
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_text(json.dumps(_sample_inputs()))

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(inputs_path), "--income", "200000"],
        capture_output=True, text=True, check=True,
    )
    assert "200000" not in result.stdout
    assert "income_annual" not in result.stdout
    assert "ratio" not in result.stdout
    parsed = json.loads(result.stdout)
    qualification = parsed["units"]["tower2-2207"]["qualification"]
    assert qualification["qualifies"] in (True, False)
    assert set(qualification.keys()) == {"required_income", "qualifies"}


def test_cli_rejects_income_annual_inside_inputs_json_file(tmp_path):
    # The channel for income is the --income flag, never a field written
    # into inputs.json itself - a build step that put it there must fail
    # loudly, not silently persist it.
    inputs_path = tmp_path / "inputs.json"
    data = _sample_inputs()
    data["income_annual"] = 200000
    inputs_path.write_text(json.dumps(data))

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(inputs_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "error:" in result.stderr
    assert "income_annual" in result.stderr


def test_cli_malformed_input_exits_nonzero_with_message(tmp_path):
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_text(json.dumps({}))

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(inputs_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "error:" in result.stderr


def test_cli_wrong_argv_usage(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "usage" in result.stderr
