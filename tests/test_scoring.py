"""Tests for scoring.py - spec-driven scoring, distress, climate, ask.

No network calls anywhere in this module; load_spec() only reads local
files (the committed example spec plus small in-test fixtures).
"""

from __future__ import annotations

import math
import pathlib

import pytest

import scoring

EXAMPLE_SPEC_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "examples" / "scoring.example.yml"
)


def _minimal_spec():
    return {
        "version": 1,
        "dimensions": {
            "commute": {
                "weight": 0.5,
                "rank_based": False,
                "anchors": [
                    {"score_min": 1, "score_max": 10, "meaning": "worse"},
                    {"score_min": 11, "score_max": 20, "meaning": "better"},
                ],
            },
            "budget_fit": {
                "weight": 0.5,
                "rank_based": False,
                "anchors": [
                    {"score_min": 1, "score_max": 10, "meaning": "worse"},
                    {"score_min": 11, "score_max": 20, "meaning": "better"},
                ],
            },
        },
        "action_bands": {"tour_now_min": 80, "watch_min": 50},
        "calibration": {"tour_now_max_share": 0.15},
        "distress": {
            "stale_factor": 1.5,
            "severity": [
                {"flag": "relisted", "points": 2, "meaning": "came back on market"},
                {"flag": "price_cuts_30d", "points": 3, "meaning": "two-plus cuts"},
                {"flag": "stale", "points": 1, "meaning": "beyond median days"},
            ],
        },
        "climate": {"min_sample": 5},
    }


# ---------------------------------------------------------------------------
# load_spec / validation
# ---------------------------------------------------------------------------


def test_load_spec_reads_committed_example():
    spec = scoring.load_spec(EXAMPLE_SPEC_PATH)

    assert spec["version"] == 1
    assert set(spec["dimensions"]) == {
        "commute",
        "budget_fit",
        "safety",
        "cleanliness",
        "move_in_window_fit",
        "walkability",
    }
    total_weight = sum(d["weight"] for d in spec["dimensions"].values())
    assert math.isclose(total_weight, 1.0, abs_tol=1e-6)


def test_load_spec_rejects_weights_not_summing_to_one(tmp_path):
    import yaml

    spec = _minimal_spec()
    spec["dimensions"]["commute"]["weight"] = 0.3  # 0.3 + 0.5 = 0.8, not 1.0
    path = tmp_path / "bad-weights.yml"
    path.write_text(yaml.safe_dump(spec))

    with pytest.raises(ValueError, match="sum to 1.0"):
        scoring.load_spec(path)


def test_load_spec_rejects_gapped_anchor_bands(tmp_path):
    import yaml

    spec = _minimal_spec()
    spec["dimensions"]["commute"]["anchors"] = [
        {"score_min": 1, "score_max": 10, "meaning": "worse"},
        {"score_min": 15, "score_max": 20, "meaning": "better"},  # gap 11-14
    ]
    path = tmp_path / "gapped.yml"
    path.write_text(yaml.safe_dump(spec))

    with pytest.raises(ValueError, match="gap"):
        scoring.load_spec(path)


def test_load_spec_rejects_overlapping_anchor_bands(tmp_path):
    import yaml

    spec = _minimal_spec()
    spec["dimensions"]["commute"]["anchors"] = [
        {"score_min": 1, "score_max": 12, "meaning": "worse"},
        {"score_min": 10, "score_max": 20, "meaning": "better"},  # overlaps 10-12
    ]
    path = tmp_path / "overlap.yml"
    path.write_text(yaml.safe_dump(spec))

    with pytest.raises(ValueError, match="overlap"):
        scoring.load_spec(path)


def test_load_spec_rejects_bad_action_bands(tmp_path):
    import yaml

    spec = _minimal_spec()
    spec["action_bands"] = {"tour_now_min": 40, "watch_min": 60}  # inverted
    path = tmp_path / "bad-bands.yml"
    path.write_text(yaml.safe_dump(spec))

    with pytest.raises(ValueError, match="tour_now_min"):
        scoring.load_spec(path)


def test_load_spec_missing_pyyaml_raises_clear_error(monkeypatch, tmp_path):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("no module named yaml")
        return real_import(name, *args, **kwargs)

    path = tmp_path / "irrelevant.yml"
    path.write_text("version: 1\n")

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="pip install pyyaml"):
        scoring.load_spec(path)


# ---------------------------------------------------------------------------
# score_unit
# ---------------------------------------------------------------------------


def test_score_unit_full_marks_all_dimensions():
    spec = _minimal_spec()
    result = scoring.score_unit({"commute": 20, "budget_fit": 20}, spec)

    assert result["composite"] == 100.0
    assert result["band"] == "TourNow"
    assert result["renormalized"] == []
    assert "renormalization_note" not in result


def test_score_unit_worst_marks_are_skip():
    spec = _minimal_spec()
    result = scoring.score_unit({"commute": 1, "budget_fit": 1}, spec)

    assert result["composite"] == 0.0
    assert result["band"] == "Skip"


def test_score_unit_watch_band_midrange():
    spec = _minimal_spec()
    # dimension range is 1-20 for both dims. commute=10 -> (10-1)/19 = 0.4737;
    # budget_fit=20 -> (20-1)/19 = 1.0. Weighted 50/50 -> 0.73684 -> 73.7,
    # which is Watch (50 <= 73.7 < 80).
    result = scoring.score_unit({"commute": 10, "budget_fit": 20}, spec)

    assert result["composite"] == 73.7
    assert result["band"] == "Watch"


def test_score_unit_missing_dimension_renormalizes_weight():
    spec = _minimal_spec()
    result = scoring.score_unit({"commute": 20, "budget_fit": None}, spec)

    # only commute available; its weight (0.5) renormalizes to 1.0 of the
    # composite, and commute=20 is the top of its range -> fraction 1.0
    assert result["composite"] == 100.0
    assert result["renormalized"] == ["budget_fit"]
    assert "weights renormalized" in result["renormalization_note"]
    assert "1 of 2" in result["renormalization_note"]


def test_score_unit_missing_dimension_absent_from_dict_treated_as_missing():
    spec = _minimal_spec()
    result = scoring.score_unit({"commute": 20}, spec)

    assert result["renormalized"] == ["budget_fit"]
    assert result["composite"] == 100.0


def test_score_unit_all_missing_is_unscored():
    spec = _minimal_spec()
    result = scoring.score_unit({"commute": None, "budget_fit": None}, spec)

    assert result["composite"] is None
    assert result["band"] == "n/a - unscored"
    assert set(result["renormalized"]) == {"commute", "budget_fit"}
    assert "renormalization_note" in result


def test_score_unit_rank_based_uses_tier_position_not_magnitude():
    spec = _minimal_spec()
    spec["dimensions"]["commute"]["rank_based"] = True
    spec["dimensions"]["commute"]["anchors"] = [
        {"score_min": 1, "score_max": 5, "meaning": "tier0"},
        {"score_min": 6, "score_max": 10, "meaning": "tier1"},
        {"score_min": 11, "score_max": 20, "meaning": "tier2"},
    ]
    # score=6 (bottom of tier1, 3 tiers -> idx 1 of 0..2) should score the
    # same fraction as score=10 (top of tier1): both land in tier index 1,
    # fraction = 1/2 = 0.5, regardless of where in the tier the raw value sits.
    low_in_tier = scoring.score_unit({"commute": 6, "budget_fit": None}, spec)
    high_in_tier = scoring.score_unit({"commute": 10, "budget_fit": None}, spec)

    assert low_in_tier["composite"] == high_in_tier["composite"] == 50.0


def test_score_unit_clamps_out_of_range_values():
    spec = _minimal_spec()
    # 999 is above the tiled range (max 20); must clamp to top tier, not raise.
    result = scoring.score_unit({"commute": 999, "budget_fit": 20}, spec)
    assert result["composite"] == 100.0


def test_score_unit_fractional_score_in_integer_gap_does_not_crash():
    # Reproduces the reported defect verbatim: the committed example spec's
    # commute dimension tiles 13-16 then 17-20 (a permitted 1-unit integer
    # gap). 16.5 falls in that gap and previously raised ValueError instead
    # of scoring.
    spec = scoring.load_spec(EXAMPLE_SPEC_PATH)
    result = scoring.score_unit(
        {
            "commute": 16.5,
            "budget_fit": 20,
            "safety": 20,
            "cleanliness": 20,
            "move_in_window_fit": 20,
        },
        spec,
    )
    assert isinstance(result["composite"], float)
    assert result["band"] in ("TourNow", "Watch", "Skip")


def test_score_unit_fractional_score_never_raises_across_the_full_range():
    spec = _minimal_spec()
    spec["dimensions"]["commute"]["anchors"] = [
        {"score_min": 1, "score_max": 8, "meaning": "worst"},
        {"score_min": 9, "score_max": 12, "meaning": "mid-low"},
        {"score_min": 13, "score_max": 16, "meaning": "mid-high"},
        {"score_min": 17, "score_max": 20, "meaning": "best"},
    ]
    # Sweep every tenth across the full tiled range, including every gap
    # midpoint (8.5, 12.5, 16.5) - none of these should raise.
    value = 1.0
    while value <= 20.0:
        scoring.score_unit({"commute": value, "budget_fit": 20}, spec)
        value = round(value + 0.1, 1)


def test_anchor_index_snaps_to_nearer_band_in_a_permitted_gap():
    anchors = [
        {"score_min": 1, "score_max": 8, "meaning": "a"},
        {"score_min": 9, "score_max": 12, "meaning": "b"},
        {"score_min": 13, "score_max": 16, "meaning": "c"},
        {"score_min": 17, "score_max": 20, "meaning": "d"},
    ]
    assert scoring._anchor_index(16.4, anchors) == 2  # closer to band c
    assert scoring._anchor_index(16.6, anchors) == 3  # closer to band d


def test_anchor_index_tie_at_gap_midpoint_resolves_to_higher_band():
    anchors = [
        {"score_min": 1, "score_max": 8, "meaning": "a"},
        {"score_min": 9, "score_max": 12, "meaning": "b"},
        {"score_min": 13, "score_max": 16, "meaning": "c"},
        {"score_min": 17, "score_max": 20, "meaning": "d"},
    ]
    # 16.5 is exactly equidistant between band c (ends 16) and band d
    # (starts 17); the documented tie-break favors the higher band.
    assert scoring._anchor_index(16.5, anchors) == 3
    assert scoring._anchor_index(8.5, anchors) == 1


def test_score_unit_rank_based_fractional_gap_value_uses_snapped_tier():
    spec = _minimal_spec()
    spec["dimensions"]["commute"]["rank_based"] = True
    spec["dimensions"]["commute"]["anchors"] = [
        {"score_min": 1, "score_max": 8, "meaning": "tier0"},
        {"score_min": 9, "score_max": 12, "meaning": "tier1"},
        {"score_min": 13, "score_max": 16, "meaning": "tier2"},
        {"score_min": 17, "score_max": 20, "meaning": "tier3"},
    ]
    # 16.5 snaps to tier3 (idx 3 of 0..3) -> fraction 1.0, same as any
    # score cleanly inside tier3.
    snapped = scoring.score_unit({"commute": 16.5, "budget_fit": None}, spec)
    clean = scoring.score_unit({"commute": 17, "budget_fit": None}, spec)
    assert snapped["composite"] == clean["composite"] == 100.0


# ---------------------------------------------------------------------------
# band_with_gates
# ---------------------------------------------------------------------------


def test_band_with_gates_demotes_tour_now_when_blocked():
    assert scoring.band_with_gates("TourNow", tour_now_blocked=True) == "Watch"


def test_band_with_gates_leaves_tour_now_when_not_blocked():
    assert scoring.band_with_gates("TourNow", tour_now_blocked=False) == "TourNow"


def test_band_with_gates_leaves_watch_and_skip_unchanged():
    assert scoring.band_with_gates("Watch", tour_now_blocked=True) == "Watch"
    assert scoring.band_with_gates("Skip", tour_now_blocked=True) == "Skip"


# ---------------------------------------------------------------------------
# calibrate
# ---------------------------------------------------------------------------


def test_calibrate_ok_when_under_target():
    spec = _minimal_spec()
    bands = ["TourNow"] + ["Watch"] * 9  # 1/10 = 10% <= 15% target
    result = scoring.calibrate(bands, spec)

    assert result["share_tour_now"] == 0.1
    assert result["ok"] is True
    assert result["warning"] is None
    assert result["sample_size"] == 10


def test_calibrate_warns_when_over_target():
    spec = _minimal_spec()
    bands = ["TourNow"] * 3 + ["Watch"] * 7  # 30% > 15% target
    result = scoring.calibrate(bands, spec)

    assert result["ok"] is False
    assert result["warning"] is not None
    assert "TourNow" in result["warning"]


def test_calibrate_empty_bands_is_ok():
    spec = _minimal_spec()
    result = scoring.calibrate([], spec)

    assert result["sample_size"] == 0
    assert result["ok"] is True
    assert result["warning"] is None


# ---------------------------------------------------------------------------
# distress_flags
# ---------------------------------------------------------------------------


def test_distress_flags_relisted():
    spec = _minimal_spec()
    events = {
        "relist_count": 1,
        "cuts_30d": 0,
        "days_listed": 5,
        "neighborhood_median_days": 20,
    }
    flags = scoring.distress_flags(events, spec)

    ids = [f["flag"] for f in flags]
    assert ids == ["relisted"]
    assert flags[0]["severity"] == 2


def test_distress_flags_price_cuts_needs_two_or_more():
    spec = _minimal_spec()
    one_cut = {
        "relist_count": 0,
        "cuts_30d": 1,
        "days_listed": None,
        "neighborhood_median_days": None,
    }
    two_cuts = dict(one_cut, cuts_30d=2)

    assert scoring.distress_flags(one_cut, spec) == []
    flags = scoring.distress_flags(two_cuts, spec)
    assert [f["flag"] for f in flags] == ["price_cuts_30d"]


def test_distress_flags_stale_uses_configured_factor():
    spec = _minimal_spec()
    spec["distress"]["stale_factor"] = 2.0
    events = {
        "relist_count": 0,
        "cuts_30d": 0,
        "days_listed": 39,
        "neighborhood_median_days": 20,  # threshold = 40; 39 does not exceed
    }
    assert scoring.distress_flags(events, spec) == []

    events["days_listed"] = 41
    flags = scoring.distress_flags(events, spec)
    assert [f["flag"] for f in flags] == ["stale"]


def test_distress_flags_none_fields_never_trigger():
    spec = _minimal_spec()
    events = {
        "relist_count": None,
        "cuts_30d": None,
        "days_listed": None,
        "neighborhood_median_days": None,
    }
    assert scoring.distress_flags(events, spec) == []


def test_distress_flags_multiple_flags_and_order():
    spec = _minimal_spec()
    events = {
        "relist_count": 2,
        "cuts_30d": 3,
        "days_listed": 100,
        "neighborhood_median_days": 20,
    }
    flags = scoring.distress_flags(events, spec)
    assert [f["flag"] for f in flags] == ["relisted", "price_cuts_30d", "stale"]


def test_distress_flags_missing_severity_row_still_returns_flag():
    spec = _minimal_spec()
    spec["distress"]["severity"] = []  # no committed rows at all
    events = {
        "relist_count": 1,
        "cuts_30d": 0,
        "days_listed": None,
        "neighborhood_median_days": None,
    }
    flags = scoring.distress_flags(events, spec)
    assert flags[0]["flag"] == "relisted"
    assert flags[0]["severity"] is None


def test_distress_flags_no_scam_or_fraud_language():
    spec = _minimal_spec()
    for row in spec["distress"]["severity"]:
        meaning = row["meaning"].lower()
        assert "scam" not in meaning
        assert "fraud" not in meaning


# ---------------------------------------------------------------------------
# move_in_fit_score
# ---------------------------------------------------------------------------


def _spec_with_move_in_fit():
    spec = _minimal_spec()
    spec["move_in_fit"] = {
        "perfect_max_days": 0,
        "minor_max_days": 14,
        "moderate_max_days": 42,
    }
    return spec


def test_move_in_fit_score_perfect_overlap():
    spec = _spec_with_move_in_fit()
    result = scoring.move_in_fit_score("2026-08-05", "2026-08-01", "2026-08-15", spec)
    assert result == 20.0


def test_move_in_fit_score_boundary_inclusive_on_window_edges():
    spec = _spec_with_move_in_fit()
    assert scoring.move_in_fit_score("2026-08-01", "2026-08-01", "2026-08-15", spec) == 20.0
    assert scoring.move_in_fit_score("2026-08-15", "2026-08-01", "2026-08-15", spec) == 20.0


def test_move_in_fit_score_minor_gap_before_window():
    spec = _spec_with_move_in_fit()
    # 2026-07-25 is 7 days before the window starts (2026-08-01) -> minor tier.
    result = scoring.move_in_fit_score("2026-07-25", "2026-08-01", "2026-08-15", spec)
    assert result == 16.0


def test_move_in_fit_score_minor_gap_after_window():
    spec = _spec_with_move_in_fit()
    # 2026-08-20 is 5 days after the window ends (2026-08-15) -> minor tier.
    result = scoring.move_in_fit_score("2026-08-20", "2026-08-01", "2026-08-15", spec)
    assert result == 16.0


def test_move_in_fit_score_moderate_gap():
    spec = _spec_with_move_in_fit()
    # 30 days before the window start -> moderate tier (15-42 days).
    result = scoring.move_in_fit_score("2026-07-02", "2026-08-01", "2026-08-15", spec)
    assert result == 12.0


def test_move_in_fit_score_outright_miss():
    spec = _spec_with_move_in_fit()
    # 60 days after the window ends -> beyond moderate_max_days -> miss tier.
    result = scoring.move_in_fit_score("2026-10-14", "2026-08-01", "2026-08-15", spec)
    assert result == 8.0


def test_move_in_fit_score_gap_day_thresholds_are_boundary_inclusive():
    spec = _spec_with_move_in_fit()
    # Exactly 14 days before the window -> still minor tier (<=14).
    assert scoring.move_in_fit_score("2026-07-18", "2026-08-01", "2026-08-15", spec) == 16.0
    # 15 days before the window -> tips into moderate tier.
    assert scoring.move_in_fit_score("2026-07-17", "2026-08-01", "2026-08-15", spec) == 12.0
    # Exactly 42 days before the window -> still moderate tier (<=42).
    forty_two_days_before = "2026-06-20"
    assert (
        scoring.move_in_fit_score(forty_two_days_before, "2026-08-01", "2026-08-15", spec)
        == 12.0
    )


def test_move_in_fit_score_missing_available_date_is_none():
    spec = _spec_with_move_in_fit()
    assert scoring.move_in_fit_score(None, "2026-08-01", "2026-08-15", spec) is None


def test_move_in_fit_score_missing_window_is_none():
    spec = _spec_with_move_in_fit()
    assert scoring.move_in_fit_score("2026-08-05", None, "2026-08-15", spec) is None
    assert scoring.move_in_fit_score("2026-08-05", "2026-08-01", None, spec) is None


def test_move_in_fit_score_unparseable_date_is_none_not_a_crash():
    spec = _spec_with_move_in_fit()
    assert scoring.move_in_fit_score("not-a-date", "2026-08-01", "2026-08-15", spec) is None
    assert scoring.move_in_fit_score("TBD", "2026-08-01", "2026-08-15", spec) is None


def test_move_in_fit_score_accepts_date_objects():
    import datetime

    spec = _spec_with_move_in_fit()
    result = scoring.move_in_fit_score(
        datetime.date(2026, 8, 5),
        datetime.date(2026, 8, 1),
        datetime.date(2026, 8, 15),
        spec,
    )
    assert result == 20.0


def test_move_in_fit_score_uses_code_defaults_when_spec_omits_thresholds():
    spec = _minimal_spec()  # no "move_in_fit" key at all
    result = scoring.move_in_fit_score("2026-08-05", "2026-08-01", "2026-08-15", spec)
    assert result == 20.0
    result_none_spec = scoring.move_in_fit_score(
        "2026-08-05", "2026-08-01", "2026-08-15", spec=None
    )
    assert result_none_spec == 20.0


def test_move_in_fit_score_feeds_into_score_unit_as_a_normal_dimension():
    spec = _spec_with_move_in_fit()
    spec["dimensions"]["move_in_window_fit"] = {
        "weight": 0.34,
        "rank_based": False,
        "anchors": [
            {"score_min": 1, "score_max": 8, "meaning": "miss"},
            {"score_min": 9, "score_max": 12, "meaning": "moderate"},
            {"score_min": 13, "score_max": 16, "meaning": "minor"},
            {"score_min": 17, "score_max": 20, "meaning": "perfect"},
        ],
    }
    spec["dimensions"]["commute"]["weight"] = 0.33
    spec["dimensions"]["budget_fit"]["weight"] = 0.33

    fit = scoring.move_in_fit_score("2026-08-05", "2026-08-01", "2026-08-15", spec)
    result = scoring.score_unit(
        {"commute": 20, "budget_fit": 20, "move_in_window_fit": fit}, spec
    )
    assert result["composite"] == 100.0
    assert result["renormalized"] == []

    # A unit with no available date scores move_in_window_fit as n/a and
    # is renormalized across the other two dimensions instead.
    no_date_fit = scoring.move_in_fit_score(None, "2026-08-01", "2026-08-15", spec)
    result_missing = scoring.score_unit(
        {"commute": 20, "budget_fit": 20, "move_in_window_fit": no_date_fit}, spec
    )
    assert no_date_fit is None
    assert result_missing["composite"] == 100.0
    assert result_missing["renormalized"] == ["move_in_window_fit"]


# ---------------------------------------------------------------------------
# concession_climate
# ---------------------------------------------------------------------------


def test_concession_climate_below_min_sample():
    spec = _minimal_spec()
    stats = {"tracked": 3, "with_cuts": 1, "median_days_live": 10, "concession_mentions": 0}
    result = scoring.concession_climate(stats, spec)

    assert result["classification"] is None
    assert result["reason"] == "sample below minimum"
    assert result["sample_size"] == 3
    assert result["disclosed"] is True


def test_concession_climate_renter_favorable_by_cut_share():
    spec = _minimal_spec()
    spec["climate"] = {"min_sample": 5, "cut_share_renter_favorable": 0.5}
    stats = {"tracked": 10, "with_cuts": 6, "median_days_live": 40, "concession_mentions": 0}
    result = scoring.concession_climate(stats, spec)

    assert result["classification"] == "renter-favorable"
    assert result["sample_size"] == 10
    assert result["disclosed"] is True
    assert result["cut_share"] == 0.6


def test_concession_climate_landlord_favorable():
    spec = _minimal_spec()
    spec["climate"] = {
        "min_sample": 5,
        "cut_share_renter_favorable": 0.5,
        "concession_rate_renter_favorable": 0.4,
        "cut_share_landlord_favorable": 0.15,
        "concession_rate_landlord_favorable": 0.1,
    }
    stats = {"tracked": 20, "with_cuts": 1, "median_days_live": 8, "concession_mentions": 0}
    result = scoring.concession_climate(stats, spec)

    assert result["classification"] == "landlord-favorable"


def test_concession_climate_neutral_between_thresholds():
    spec = _minimal_spec()
    spec["climate"] = {
        "min_sample": 5,
        "cut_share_renter_favorable": 0.9,
        "concession_rate_renter_favorable": 0.9,
        "cut_share_landlord_favorable": 0.01,
        "concession_rate_landlord_favorable": 0.01,
    }
    stats = {"tracked": 10, "with_cuts": 3, "median_days_live": 20, "concession_mentions": 1}
    result = scoring.concession_climate(stats, spec)

    assert result["classification"] == "neutral"


def test_concession_climate_sample_size_always_present_on_success():
    spec = _minimal_spec()
    stats = {"tracked": 17, "with_cuts": 2, "median_days_live": 15, "concession_mentions": 1}
    result = scoring.concession_climate(stats, spec)

    assert result["sample_size"] == 17
    assert "classification" in result


# ---------------------------------------------------------------------------
# negotiation_ask
# ---------------------------------------------------------------------------


def _climate(classification="renter-favorable", n=17):
    return {"classification": classification, "sample_size": n}


def test_negotiation_ask_insufficient_data_when_unit_fields_missing():
    unit = {"cuts": None, "days_listed": 10, "current_price": 3000}
    result = scoring.negotiation_ask(unit, _climate())

    assert result["insufficient_data"] is True
    assert result["ask"] is None
    assert result["justification"] == []
    assert "unit cuts count" in result["missing"]
    assert result["reason"] == "insufficient data - no anchor computed"


def test_negotiation_ask_insufficient_data_when_climate_insufficient_sample():
    unit = {"cuts": 2, "days_listed": 40, "current_price": 3000}
    climate = {"classification": None, "reason": "sample below minimum", "sample_size": 2}
    result = scoring.negotiation_ask(unit, climate)

    assert result["insufficient_data"] is True
    assert any("climate" in m for m in result["missing"])


def test_negotiation_ask_computes_weeks_and_dollars():
    unit = {"cuts": 2, "days_listed": 38, "current_price": 3900}
    result = scoring.negotiation_ask(unit, _climate("renter-favorable", 17))

    # 2 cuts (+2) + days_listed>=30 (+1) + renter-favorable (+1) = 4 weeks
    assert result["insufficient_data"] is False
    assert result["ask"]["weeks_free"] == 4.0
    assert result["ask"]["monthly_off"] == round(4.0 * 3900 / 52, 0)
    assert any("2 price cuts observed" in j for j in result["justification"])
    assert any("38 days listed" in j for j in result["justification"])
    assert any("renter-favorable (n=17)" in j for j in result["justification"])


def test_negotiation_ask_landlord_favorable_reduces_ask_and_floors_at_zero():
    unit = {"cuts": 0, "days_listed": 5, "current_price": 3000}
    result = scoring.negotiation_ask(unit, _climate("landlord-favorable", 12))

    assert result["insufficient_data"] is False
    assert result["ask"]["weeks_free"] == 0.0
    assert result["ask"]["monthly_off"] == 0.0


def test_negotiation_ask_justification_uses_only_provided_comp():
    unit = {"cuts": 1, "days_listed": 10, "current_price": 4000}
    comps = [
        {"note": "same line, 2 floors down", "price": 3800},
        {"note": "irrelevant higher comp", "price": 4500},
    ]
    result = scoring.negotiation_ask(unit, _climate("neutral", 8), comps=comps)

    joined = " ".join(result["justification"])
    assert "3,800" in joined
    assert "same line, 2 floors down" in joined
    assert "4,500" not in joined


def test_negotiation_ask_ignores_comps_not_cheaper_than_current():
    unit = {"cuts": 1, "days_listed": 10, "current_price": 3000}
    comps = [{"note": "pricier unit", "price": 3500}]
    result = scoring.negotiation_ask(unit, _climate("neutral", 8), comps=comps)

    joined = " ".join(result["justification"])
    assert "3,500" not in joined


def test_negotiation_ask_never_produces_send_language():
    unit = {"cuts": 3, "days_listed": 60, "current_price": 5000}
    result = scoring.negotiation_ask(unit, _climate("renter-favorable", 20))

    blob = " ".join(result["justification"]).lower()
    for verb in ("send", "email", "submit", "message the landlord", "contact the landlord"):
        assert verb not in blob


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_usage_error_without_reading_files(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["scoring.py", "only-one-arg"])
    rc = scoring.main()
    captured = capsys.readouterr()

    assert rc == 2
    assert "usage" in captured.err


def test_cli_prints_score_unit_result(monkeypatch, capsys, tmp_path):
    import json as jsonlib

    dim_scores_path = tmp_path / "dims.json"
    dim_scores_path.write_text(jsonlib.dumps({"commute": 20, "budget_fit": 20, "safety": 20, "cleanliness": 20}))

    monkeypatch.setattr("sys.argv", ["scoring.py", str(EXAMPLE_SPEC_PATH), str(dim_scores_path)])
    rc = scoring.main()
    captured = capsys.readouterr()
    output = jsonlib.loads(captured.out)

    assert rc == 0
    assert output["composite"] == 100.0
    assert output["band"] == "TourNow"


def test_example_spec_with_walkability_loads():
    spec = scoring.load_spec(EXAMPLE_SPEC_PATH)
    walkability = spec["dimensions"]["walkability"]
    assert math.isclose(walkability["weight"], 0.10, abs_tol=1e-9)
    assert math.isclose(sum(d["weight"] for d in spec["dimensions"].values()), 1.0, abs_tol=1e-6)
    bands = sorted((a["score_min"], a["score_max"]) for a in walkability["anchors"])
    assert bands == [(1, 8), (9, 12), (13, 16), (17, 20)]
    import walk  # the research stage maps a Walk Score onto these bands
    for score, band in ((100, (17, 20)), (90, (17, 20)), (89, (13, 16)), (70, (13, 16)),
                        (69, (9, 12)), (50, (9, 12)), (49, (1, 8)), (0, (1, 8))):
        assert band[0] <= walk.walkability_raw(score) <= band[1], score
    assert walk.walkability_raw(None) is None


def test_missing_walkability_renormalizes_with_note():
    spec = scoring.load_spec(EXAMPLE_SPEC_PATH)
    full = {"commute": 20, "budget_fit": 20, "safety": 20, "cleanliness": 20, "move_in_window_fit": 20}
    result = scoring.score_unit(full, spec)
    assert result["renormalized"] == ["walkability"]
    assert result["renormalization_note"] == "scored on 5 of 6 dimensions; weights renormalized"
    assert result["composite"] == 100.0
