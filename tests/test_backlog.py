"""Tests for backlog.py: actions.yml sync and backlog resurfacing with decay.

Run only this file:
    .venv/bin/python -m pytest tests/test_backlog.py -q
"""

from __future__ import annotations

import copy
import json
import sys

import pytest

import backlog


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def make_unit(area="area-slug-1", building="Tower 2", unit="2207", live=True,
              price=4800, deep_link="https://example.com/u/2207"):
    record = {
        "building": building,
        "unit": unit,
        "area": area,
        "unit_deep_link": deep_link,
        "rent_verified": price,
    }
    if live is not None:
        record["live"] = live
    return record


def make_score(composite=4.5, band="TourNow"):
    return {"composite": composite, "band": band}


# --------------------------------------------------------------------------
# sync_new_units: NEW-append-only invariant
# --------------------------------------------------------------------------


def test_sync_new_units_appends_only_missing_units():
    actions = {
        "u1": {"status": "Contacted", "note": "called them", "updated_at": "2026-01-01"},
    }
    original_actions = copy.deepcopy(actions)
    verified_units = {"u1": make_unit(), "u2": make_unit(unit="2311")}

    result = backlog.sync_new_units(actions, verified_units, now="2026-07-15")

    # existing entry untouched, byte for byte
    assert result["u1"] == {"status": "Contacted", "note": "called them", "updated_at": "2026-01-01"}
    # new unit appended as NEW
    assert result["u2"] == {"status": "NEW", "note": "", "updated_at": "2026-07-15"}
    # input dict never mutated
    assert actions == original_actions


def test_sync_new_units_is_idempotent():
    actions = {}
    verified_units = {"u1": make_unit(), "u2": make_unit(unit="2311")}

    once = backlog.sync_new_units(actions, verified_units, now="2026-07-15")
    twice = backlog.sync_new_units(once, verified_units, now="2026-07-22")

    # second sync must not touch u1/u2's updated_at - they already exist
    assert twice == once


def test_sync_new_units_never_touches_note_or_status_of_existing_entry():
    actions = {
        "u1": {"status": "Skipped", "note": "too small", "updated_at": "2026-02-01"},
    }
    verified_units = {"u1": make_unit()}

    result = backlog.sync_new_units(actions, verified_units, now="2026-07-15")

    assert result["u1"]["status"] == "Skipped"
    assert result["u1"]["note"] == "too small"
    assert result["u1"]["updated_at"] == "2026-02-01"


# --------------------------------------------------------------------------
# validate_actions
# --------------------------------------------------------------------------


def test_validate_actions_accepts_all_canonical_statuses():
    actions = {f"u{i}": {"status": status, "note": "", "updated_at": "2026-01-01"}
               for i, status in enumerate(backlog.STATUSES)}
    backlog.validate_actions(actions)  # must not raise


def test_validate_actions_rejects_unknown_status():
    actions = {"u1": {"status": "Ghosted", "note": "", "updated_at": "2026-01-01"}}
    with pytest.raises(ValueError) as excinfo:
        backlog.validate_actions(actions)
    message = str(excinfo.value)
    assert "u1" in message
    assert "Ghosted" in message
    for status in backlog.STATUSES:
        assert status in message


# --------------------------------------------------------------------------
# build_backlog: status flip silences resurfacing
# --------------------------------------------------------------------------


def test_status_flip_away_from_new_silences_resurfacing():
    verified_units = {"u1": make_unit(live=True)}
    actions = {"u1": {"status": "Contacted", "note": "", "updated_at": "2026-07-01"}}
    scores = {"u1": make_score()}
    # unit already had decay history from being NEW in a prior run
    state = {"u1": {"resurfaced": 1, "last_run_id": "run-A"}}

    result = backlog.build_backlog(verified_units, actions, scores, state, run_id="run-B")

    assert result["backlog"] == []
    assert result["unscored"] == []
    assert result["stale"] == []
    # decay state removed so a later flip back to NEW starts at zero
    assert "u1" not in result["state"]


def test_missing_actions_entry_treated_as_new():
    # A unit with no actions.yml entry at all behaves exactly like NEW.
    verified_units = {"u1": make_unit(live=True)}
    actions = {}
    scores = {"u1": make_score()}
    state = {"u1": {"resurfaced": 0, "last_run_id": "run-A"}}

    result = backlog.build_backlog(verified_units, actions, scores, state, run_id="run-B")

    assert len(result["backlog"]) == 1
    assert result["backlog"][0]["unit_id"] == "u1"


# --------------------------------------------------------------------------
# build_backlog: live:false (and other non-live states) excluded
# --------------------------------------------------------------------------


def test_gone_unit_excluded_and_state_untouched():
    verified_units = {"u1": make_unit(live=False)}
    actions = {"u1": {"status": "NEW", "note": "", "updated_at": "2026-07-01"}}
    scores = {"u1": make_score()}
    state = {}

    result = backlog.build_backlog(verified_units, actions, scores, state, run_id="run-A")

    assert result["backlog"] == []
    assert result["unscored"] == []
    assert result["stale"] == []
    assert "u1" not in result["state"]


def test_recheck_liveness_status_excluded():
    unit = make_unit(live=None)
    unit["status"] = "recheck"
    verified_units = {"u1": unit}
    actions = {"u1": {"status": "NEW", "note": "", "updated_at": "2026-07-01"}}
    scores = {"u1": make_score()}

    result = backlog.build_backlog(verified_units, actions, scores, {}, run_id="run-A")

    assert result["backlog"] == []
    assert "u1" not in result["state"]


def test_unknown_liveness_excluded_never_guessed_live():
    unit = make_unit(live=None)  # no live/status field at all
    verified_units = {"u1": unit}
    actions = {"u1": {"status": "NEW", "note": "", "updated_at": "2026-07-01"}}
    scores = {"u1": make_score()}

    result = backlog.build_backlog(verified_units, actions, scores, {}, run_id="run-A")

    assert result["backlog"] == []
    assert "u1" not in result["state"]


# --------------------------------------------------------------------------
# build_backlog: first-seen gating ("news, not backlog")
# --------------------------------------------------------------------------


def test_first_run_registers_but_does_not_show():
    verified_units = {"u1": make_unit(live=True)}
    actions = {"u1": {"status": "NEW", "note": "", "updated_at": "2026-07-01"}}
    scores = {"u1": make_score()}

    result = backlog.build_backlog(verified_units, actions, scores, {}, run_id="run-A")

    assert result["backlog"] == []
    assert result["unscored"] == []
    assert result["stale"] == []
    assert result["state"]["u1"] == {"resurfaced": 0, "last_run_id": "run-A"}


def test_second_distinct_run_shows_the_unit():
    verified_units = {"u1": make_unit(live=True)}
    actions = {"u1": {"status": "NEW", "note": "", "updated_at": "2026-07-01"}}
    scores = {"u1": make_score(composite=4.8)}

    first = backlog.build_backlog(verified_units, actions, scores, {}, run_id="run-A")
    second = backlog.build_backlog(verified_units, actions, scores, first["state"], run_id="run-B")

    assert [row["unit_id"] for row in second["backlog"]] == ["u1"]
    assert second["state"]["u1"] == {"resurfaced": 1, "last_run_id": "run-B"}


def test_build_backlog_is_pure_and_does_not_mutate_its_inputs():
    verified_units = {"u1": make_unit(live=True)}
    actions = {"u1": {"status": "NEW", "note": "", "updated_at": "2026-07-01"}}
    scores = {"u1": make_score()}
    state = {"u1": {"resurfaced": 0, "last_run_id": "run-A"}}
    state_snapshot = copy.deepcopy(state)

    first = backlog.build_backlog(verified_units, actions, scores, state, run_id="run-B")
    second = backlog.build_backlog(verified_units, actions, scores, state, run_id="run-B")

    # same inputs -> same output, deterministically
    assert first == second
    # the input state dict is never mutated in place
    assert state == state_snapshot


def test_reprocessing_the_same_run_id_after_its_own_output_does_not_double_count():
    # Simulates a retried invocation for the same run_id after state.json was
    # already written: the resurface counter must not advance a second time.
    verified_units = {"u1": make_unit(live=True)}
    actions = {"u1": {"status": "NEW", "note": "", "updated_at": "2026-07-01"}}
    scores = {"u1": make_score()}
    state = {"u1": {"resurfaced": 0, "last_run_id": "run-A"}}

    first = backlog.build_backlog(verified_units, actions, scores, state, run_id="run-B")
    retried = backlog.build_backlog(verified_units, actions, scores, first["state"], run_id="run-B")

    assert first["state"]["u1"]["resurfaced"] == 1
    # retried with the already-updated state for the same run_id: no further
    # increment happens (the idempotency guard short-circuits it)
    assert retried["state"]["u1"]["resurfaced"] == 1


# --------------------------------------------------------------------------
# build_backlog: decay to stale after exactly 3 resurfacings
# --------------------------------------------------------------------------


def test_decay_demotes_to_stale_after_exactly_three_appearances():
    verified_units = {"u1": make_unit(live=True)}
    actions = {"u1": {"status": "NEW", "note": "", "updated_at": "2026-07-01"}}
    scores = {"u1": make_score(composite=5.0)}

    state: dict = {}
    run_ids = ["run-A", "run-B", "run-C", "run-D", "run-E", "run-F"]
    appearances = []
    stale_runs = []

    for run_id in run_ids:
        result = backlog.build_backlog(
            verified_units, actions, scores, state, run_id=run_id, max_resurface=3
        )
        state = result["state"]
        if result["backlog"]:
            appearances.append(run_id)
        if result["stale"]:
            stale_runs.append(run_id)

    # run-A: registers only (news, not backlog)
    # run-B, run-C, run-D: three real appearances in the backlog
    assert appearances == ["run-B", "run-C", "run-D"]
    # run-E, run-F: demoted to stale and stays there
    assert stale_runs == ["run-E", "run-F"]
    assert state["u1"]["resurfaced"] == 3


def test_stale_unit_stays_stale_until_status_changes():
    verified_units = {"u1": make_unit(live=True)}
    actions = {"u1": {"status": "NEW", "note": "", "updated_at": "2026-07-01"}}
    scores = {"u1": make_score(composite=5.0)}

    state = {"u1": {"resurfaced": 3, "last_run_id": "run-D"}}
    result = backlog.build_backlog(verified_units, actions, scores, state, run_id="run-E")
    assert result["backlog"] == []
    assert [row["unit_id"] for row in result["stale"]] == ["u1"]

    # user flips status away from NEW: state entry is removed entirely
    actions_after_flip = {"u1": {"status": "Skipped", "note": "too far", "updated_at": "2026-07-20"}}
    result2 = backlog.build_backlog(
        verified_units, actions_after_flip, scores, result["state"], run_id="run-F"
    )
    assert result2["stale"] == []
    assert "u1" not in result2["state"]


# --------------------------------------------------------------------------
# build_backlog: top_n cap, held-back units keep decay state unchanged
# --------------------------------------------------------------------------


def test_top_n_cap_holds_back_lower_ranked_without_burning_a_strike():
    verified_units = {
        "hi": make_unit(unit="1", price=5000, deep_link="https://example.com/u/1"),
        "lo": make_unit(unit="2", price=4000, deep_link="https://example.com/u/2"),
    }
    actions = {
        "hi": {"status": "NEW", "note": "", "updated_at": "2026-07-01"},
        "lo": {"status": "NEW", "note": "", "updated_at": "2026-07-01"},
    }
    scores = {"hi": make_score(composite=4.9), "lo": make_score(composite=3.1)}
    # both already survived a prior "first seen" run
    state = {
        "hi": {"resurfaced": 0, "last_run_id": "run-0"},
        "lo": {"resurfaced": 0, "last_run_id": "run-0"},
    }

    result = backlog.build_backlog(verified_units, actions, scores, state, run_id="run-1", top_n=1)

    assert [row["unit_id"] for row in result["backlog"]] == ["hi"]
    assert result["state"]["hi"] == {"resurfaced": 1, "last_run_id": "run-1"}
    # held back: decay state completely unchanged, still eligible next run -
    # ranking below the cap costs it nothing
    assert result["state"]["lo"] == {"resurfaced": 0, "last_run_id": "run-0"}

    # raising the cap next run lets the previously held-back unit through,
    # and it still only pays a single resurface strike for that appearance
    result2 = backlog.build_backlog(
        verified_units, actions, scores, result["state"], run_id="run-2", top_n=2
    )
    assert {row["unit_id"] for row in result2["backlog"]} == {"hi", "lo"}
    assert result2["state"]["lo"] == {"resurfaced": 1, "last_run_id": "run-2"}


# --------------------------------------------------------------------------
# build_backlog: unscored units
# --------------------------------------------------------------------------


def test_unscored_unit_listed_separately_never_guessed_a_rank():
    verified_units = {"u1": make_unit(live=True)}
    actions = {"u1": {"status": "NEW", "note": "", "updated_at": "2026-07-01"}}
    state = {"u1": {"resurfaced": 0, "last_run_id": "run-A"}}

    result = backlog.build_backlog(verified_units, actions, {}, state, run_id="run-B")

    assert result["backlog"] == []
    assert len(result["unscored"]) == 1
    assert result["unscored"][0]["unit_id"] == "u1"
    assert result["unscored"][0]["score"] is None
    assert result["state"]["u1"]["resurfaced"] == 1


def test_unscored_units_are_not_capped_by_top_n():
    verified_units = {
        "u1": make_unit(unit="1"),
        "u2": make_unit(unit="2"),
    }
    actions = {
        "u1": {"status": "NEW", "note": "", "updated_at": "2026-07-01"},
        "u2": {"status": "NEW", "note": "", "updated_at": "2026-07-01"},
    }
    state = {
        "u1": {"resurfaced": 0, "last_run_id": "run-0"},
        "u2": {"resurfaced": 0, "last_run_id": "run-0"},
    }

    result = backlog.build_backlog(verified_units, actions, {}, state, run_id="run-1", top_n=1)

    assert {row["unit_id"] for row in result["unscored"]} == {"u1", "u2"}


# --------------------------------------------------------------------------
# backlog_markdown
# --------------------------------------------------------------------------


def test_backlog_markdown_renders_none_when_empty():
    result = {"backlog": [], "unscored": [], "stale": [], "state": {}}
    rendered = backlog.backlog_markdown(result)
    assert "Backlog: none" in rendered


def test_backlog_markdown_renders_rows_and_stale_section():
    result = {
        "backlog": [
            {
                "unit_id": "u1",
                "area": "area-slug-1",
                "score": 4.62,
                "band": "TourNow",
                "deep_link": "https://example.com/u/1",
                "draft_outreach_note": "Hi, I'm interested in unit 1.",
            }
        ],
        "unscored": [],
        "stale": [
            {"unit_id": "u2", "score": None, "deep_link": "https://example.com/u/2"},
        ],
        "state": {},
    }
    rendered = backlog.backlog_markdown(result)
    assert "u1" in rendered
    assert "4.62" in rendered
    assert "TourNow" in rendered
    assert "https://example.com/u/1" in rendered
    assert "Draft outreach:" in rendered
    assert "### Stale backlog" in rendered
    assert "u2" in rendered


# --------------------------------------------------------------------------
# describe_sync
# --------------------------------------------------------------------------


def test_describe_sync_reports_creation_appended_and_no_changes():
    verified_units = {"u1": make_unit(), "u2": make_unit(unit="2")}

    created = backlog.sync_new_units({}, verified_units, now="2026-07-15")
    assert "created" in backlog.describe_sync({}, created)

    more_units = {"u1": make_unit(), "u2": make_unit(unit="2"), "u3": make_unit(unit="3")}
    appended = backlog.sync_new_units(created, more_units, now="2026-07-16")
    summary = backlog.describe_sync(created, appended)
    assert "1 NEW" in summary
    assert "3 units tracked" in summary

    unchanged = backlog.sync_new_units(appended, more_units, now="2026-07-17")
    assert backlog.describe_sync(appended, unchanged) == "actions.yml: no changes"


# --------------------------------------------------------------------------
# unit_key
# --------------------------------------------------------------------------


def test_unit_key_prefers_explicit_id():
    unit = {"unit_id": "explicit-id", "building": "Tower 2", "unit": "2207"}
    assert backlog.unit_key(unit) == "explicit-id"


def test_unit_key_falls_back_to_slug():
    unit = {"building": "Example Tower 2", "unit": "2207"}
    assert backlog.unit_key(unit) == "example-tower-2-2207"


# --------------------------------------------------------------------------
# append_new_entries: text append, never a parse-and-rewrite
# --------------------------------------------------------------------------


def test_append_new_entries_preserves_comments_and_hand_chosen_order(tmp_path):
    actions_path = tmp_path / "actions.yml"
    original_text = (
        "# call back after 5pm\n"
        "u2:\n"
        "  status: Contacted\n"
        "  note: \"called them\"\n"
        "  updated_at: \"2026-01-01\"\n"
        "u1:\n"
        "  status: Skipped\n"
        "  note: \"too small\"\n"
        "  updated_at: \"2026-02-01\"\n"
    )
    actions_path.write_text(original_text)

    before = backlog.load_actions(actions_path)
    verified_units = {
        "u1": make_unit(unit="1"),
        "u2": make_unit(unit="2"),
        "u3": make_unit(unit="3"),
    }
    after = backlog.sync_new_units(before, verified_units, now="2026-07-15")

    wrote = backlog.append_new_entries(actions_path, before, after)
    assert wrote is True

    new_text = actions_path.read_text()
    # every byte of the original content survives unchanged, comment and
    # non-alphabetical key order included - this is an append, not a rewrite
    assert new_text.startswith(original_text)
    assert "# call back after 5pm" in new_text
    assert "u3" in new_text

    reloaded = backlog.load_actions(actions_path)
    assert reloaded["u1"] == {"status": "Skipped", "note": "too small", "updated_at": "2026-02-01"}
    assert reloaded["u2"] == {
        "status": "Contacted",
        "note": "called them",
        "updated_at": "2026-01-01",
    }
    assert reloaded["u3"] == {"status": "NEW", "note": "", "updated_at": "2026-07-15"}


def test_append_new_entries_noop_leaves_file_untouched_when_nothing_new(tmp_path):
    actions_path = tmp_path / "actions.yml"
    original_text = "u1:\n  status: NEW\n  note: \"\"\n  updated_at: \"2026-07-01\"\n"
    actions_path.write_text(original_text)

    before = backlog.load_actions(actions_path)
    verified_units = {"u1": make_unit(unit="1")}
    after = backlog.sync_new_units(before, verified_units, now="2026-07-15")

    wrote = backlog.append_new_entries(actions_path, before, after)

    assert wrote is False
    assert actions_path.read_text() == original_text


def test_append_new_entries_across_repeated_scan_and_hydrate_runs(tmp_path):
    # Simulates the exact acceptance criterion: a hand-authored file with a
    # deliberate status and a comment must survive repeated runs, gaining
    # only appended NEW entries.
    actions_path = tmp_path / "actions.yml"
    original_text = (
        "# do not contact until after the holiday\n"
        "u5:\n"
        "  status: ToContact\n"
        "  note: \"waiting\"\n"
        "  updated_at: \"2026-06-01\"\n"
    )
    actions_path.write_text(original_text)

    # scan run 1: discovers u5 (already tracked) and u6 (new)
    before1 = backlog.load_actions(actions_path)
    verified1 = {"u5": make_unit(unit="5"), "u6": make_unit(unit="6")}
    after1 = backlog.sync_new_units(before1, verified1, now="2026-07-01")
    backlog.append_new_entries(actions_path, before1, after1)

    text_after_run1 = actions_path.read_text()
    assert text_after_run1.startswith(original_text)
    assert "u6" in text_after_run1

    # hydrate run 2: same units, nothing new - file must not change at all
    before2 = backlog.load_actions(actions_path)
    after2 = backlog.sync_new_units(before2, verified1, now="2026-07-08")
    backlog.append_new_entries(actions_path, before2, after2)
    assert actions_path.read_text() == text_after_run1

    # scan run 3: one more new unit appears
    before3 = backlog.load_actions(actions_path)
    verified3 = {"u5": make_unit(unit="5"), "u6": make_unit(unit="6"), "u7": make_unit(unit="7")}
    after3 = backlog.sync_new_units(before3, verified3, now="2026-07-15")
    backlog.append_new_entries(actions_path, before3, after3)

    final_text = actions_path.read_text()
    assert final_text.startswith(text_after_run1)
    assert "u7" in final_text
    # the original hand-set content is still intact byte for byte
    assert "# do not contact until after the holiday" in final_text
    final = backlog.load_actions(actions_path)
    assert final["u5"] == {"status": "ToContact", "note": "waiting", "updated_at": "2026-06-01"}


def test_append_new_entries_creates_file_when_missing(tmp_path):
    actions_path = tmp_path / "actions.yml"
    assert not actions_path.exists()

    before = {}
    verified_units = {"u1": make_unit(unit="1")}
    after = backlog.sync_new_units(before, verified_units, now="2026-07-15")

    wrote = backlog.append_new_entries(actions_path, before, after)

    assert wrote is True
    assert actions_path.exists()
    reloaded = backlog.load_actions(actions_path)
    assert reloaded["u1"]["status"] == "NEW"


# --------------------------------------------------------------------------
# unit_key: join-key style consistency
# --------------------------------------------------------------------------


def test_unit_key_style_does_not_match_verify_units_example_key():
    # Regression guard for the actions.md join-key documentation defect:
    # unit_key()'s fallback slug must NOT collide with verify_units.py's
    # compact "tower2-2207" example key style for the same building/unit.
    unit = {"building": "Tower 2", "unit": "2207"}
    assert backlog.unit_key(unit) == "tower-2-2207"
    assert backlog.unit_key(unit) != "tower2-2207"


# --------------------------------------------------------------------------
# CLI end to end (tmp_path, no network)
# --------------------------------------------------------------------------


def test_cli_end_to_end_writes_actions_and_state(tmp_path, monkeypatch, capsys):
    verified_path = tmp_path / "verified.json"
    actions_path = tmp_path / "actions.yml"
    scores_path = tmp_path / "scores.json"
    state_path = tmp_path / "backlog-state.json"

    verified_path.write_text(json.dumps([
        {"building": "Tower 2", "unit": "2207", "area": "area-slug-1",
         "live": True, "unit_deep_link": "https://example.com/u/2207",
         "rent_verified": 4800},
    ]))
    scores_path.write_text(json.dumps({
        "tower-2-2207": {"composite": 4.7, "band": "TourNow"},
    }))

    base_argv = [
        "backlog.py",
        "--verified", str(verified_path),
        "--actions", str(actions_path),
        "--scores", str(scores_path),
        "--state", str(state_path),
    ]
    monkeypatch.setattr(sys, "argv", base_argv + ["--run-id", "run-A", "--now", "2026-07-15"])

    rc = backlog.main()
    assert rc == 0

    # actions.yml created with the unit initialized as NEW
    assert actions_path.exists()
    created = backlog.load_actions(actions_path)
    assert created["tower-2-2207"]["status"] == "NEW"

    # state file written (unit registered but not yet shown - first run)
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert state["tower-2-2207"] == {"resurfaced": 0, "last_run_id": "run-A"}

    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    assert payload["backlog"] == []
    assert "Backlog: none" in payload["markdown"]

    # second run: unit should now surface in the backlog
    monkeypatch.setattr(sys, "argv", base_argv + ["--run-id", "run-B", "--now", "2026-07-22"])
    rc2 = backlog.main()
    assert rc2 == 0
    stdout2 = capsys.readouterr().out
    payload2 = json.loads(stdout2)
    assert [row["unit_id"] for row in payload2["backlog"]] == ["tower-2-2207"]

    # actions.yml entry for the unit was never rewritten by the second run
    after_second = backlog.load_actions(actions_path)
    assert after_second["tower-2-2207"]["status"] == "NEW"


def test_cli_preserves_hand_authored_actions_yml_across_scan_and_hydrate(
    tmp_path, monkeypatch, capsys
):
    # End-to-end regression for the "survives repeated scan and hydrate
    # runs unchanged except for appended NEW entries" acceptance criterion,
    # driven through the actual CLI entry point (not just the library
    # functions) against a file with a hand-set status and a comment.
    verified_path = tmp_path / "verified.json"
    actions_path = tmp_path / "actions.yml"
    state_path = tmp_path / "backlog-state.json"

    hand_authored = (
        "# call back after 5pm\n"
        "tower-2-2311:\n"
        "  status: Contacted\n"
        "  note: \"left a voicemail\"\n"
        "  updated_at: \"2026-07-01\"\n"
    )
    actions_path.write_text(hand_authored)

    verified_path.write_text(json.dumps([
        {"building": "Tower 2", "unit": "2311", "area": "area-slug-1",
         "live": True, "unit_deep_link": "https://example.com/u/2311",
         "rent_verified": 4700},
        {"building": "Tower 2", "unit": "2207", "area": "area-slug-1",
         "live": True, "unit_deep_link": "https://example.com/u/2207",
         "rent_verified": 4800},
    ]))

    base_argv = [
        "backlog.py",
        "--verified", str(verified_path),
        "--actions", str(actions_path),
        "--state", str(state_path),
    ]

    # scan run: discovers the new tower-2-2207 unit
    monkeypatch.setattr(sys, "argv", base_argv + ["--run-id", "scan-A", "--now", "2026-07-15"])
    assert backlog.main() == 0
    capsys.readouterr()

    after_scan = actions_path.read_text()
    assert after_scan.startswith(hand_authored)
    assert "# call back after 5pm" in after_scan
    assert "tower-2-2207" in after_scan

    # hydrate run: same units, nothing new - file must be byte-for-byte
    # identical to right after the scan run
    monkeypatch.setattr(sys, "argv", base_argv + ["--run-id", "hyd-A", "--now", "2026-07-16"])
    assert backlog.main() == 0
    capsys.readouterr()
    assert actions_path.read_text() == after_scan

    # the hand-set status and note are still exactly what the user wrote
    final = backlog.load_actions(actions_path)
    assert final["tower-2-2311"] == {
        "status": "Contacted",
        "note": "left a voicemail",
        "updated_at": "2026-07-01",
    }
    assert final["tower-2-2207"]["status"] == "NEW"
