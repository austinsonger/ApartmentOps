"""Tests for the snapshot ledger and delta engine (epic E2).

Run with: python -m pytest tests/test_snapshots.py -q (from the repo root)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from snapshots import (
    DEFAULT_DROP_PCT,
    DEFAULT_DROP_WINDOW_DAYS,
    InsufficientHistoryError,
    LedgerError,
    append_rows,
    append_runlog,
    detect_drops,
    diff_last_two,
    digest_markdown,
    heartbeat_overdue,
    load_runs,
    rows_for_run,
    sanity_check,
    trajectory,
)

SCRIPT = Path(__file__).resolve().parent.parent / ".claude" / "skills" / "apartmentops" / "scripts" / "snapshots.py"


def _row(unit_id, run_id, observed_at, price=2500, availability="2026-08-01", status="live", url=None, fetch_evidence=None):
    return {
        "unit_id": unit_id,
        "url": url or f"https://example.com/{unit_id}",
        "observed_at": observed_at,
        "run_id": run_id,
        "price": price,
        "availability": availability,
        "status": status,
        "fetch_evidence": fetch_evidence,
    }


# --------------------------------------------------------------------------
# append_rows: validation + append-only guarantees
# --------------------------------------------------------------------------


def test_append_rows_happy_path_two_runs(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    rows1 = [
        _row("tower2-2207", "hyd-1", "2026-07-01T10:00:00-04:00", price=2500),
        _row("tower2-2208", "hyd-1", "2026-07-01T10:00:05-04:00", price=2600),
    ]
    n1 = append_rows(path, "hyd-1", rows1)
    assert n1 == 2

    rows2 = [
        _row("tower2-2207", "hyd-2", "2026-07-08T10:00:00-04:00", price=2450),
        _row("tower2-2208", "hyd-2", "2026-07-08T10:00:05-04:00", price=2600),
    ]
    n2 = append_rows(path, "hyd-2", rows2)
    assert n2 == 2

    runs = load_runs(path)
    assert runs == ["hyd-1", "hyd-2"]
    assert len(rows_for_run(path, "hyd-1")) == 2
    assert len(rows_for_run(path, "hyd-2")) == 2


def test_append_rows_is_strictly_append_only(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    append_rows(path, "hyd-1", [_row("a", "hyd-1", "2026-07-01T10:00:00-04:00")])
    first_line = path.read_text().splitlines()[0]

    append_rows(path, "hyd-2", [_row("a", "hyd-2", "2026-07-08T10:00:00-04:00")])
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert lines[0] == first_line  # first line untouched by the second append


def test_append_rows_no_run_confirms_null_never_guessed(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    row = _row("a", "hyd-1", "2026-07-01T10:00:00-04:00", price=None, availability=None, status="recheck")
    append_rows(path, "hyd-1", [row])
    stored = rows_for_run(path, "hyd-1")[0]
    assert stored["price"] is None
    assert stored["availability"] is None
    assert stored["status"] == "recheck"


@pytest.mark.parametrize(
    "mutate,message_fragment",
    [
        (lambda r: r.pop("price"), "missing required field"),
        (lambda r: r.update(status="deleted"), "status must be one of"),
        (lambda r: r.update(price="2500"), "price must be a number"),
        (lambda r: r.update(price=True), "price must be a number"),
        (lambda r: r.update(availability=123), "availability must be a string"),
        (lambda r: r.update(fetch_evidence=123), "fetch_evidence must be a string"),
        (lambda r: r.update(unit_id=""), "unit_id must be a non-empty string"),
        (lambda r: r.update(run_id="hyd-999"), "does not match the append run_id"),
    ],
)
def test_append_rows_rejects_invalid_rows(tmp_path, mutate, message_fragment):
    path = tmp_path / "snapshots.jsonl"
    row = _row("a", "hyd-1", "2026-07-01T10:00:00-04:00")
    mutate(row)
    with pytest.raises(ValueError, match=message_fragment):
        append_rows(path, "hyd-1", [row])
    # nothing was written - validation happens before any write
    assert not path.exists()


def test_append_rows_batch_is_all_or_nothing(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    good = _row("a", "hyd-1", "2026-07-01T10:00:00-04:00")
    bad = _row("b", "hyd-1", "2026-07-01T10:00:01-04:00")
    bad["status"] = "not-a-status"
    with pytest.raises(ValueError):
        append_rows(path, "hyd-1", [good, bad])
    assert not path.exists()


def test_append_rows_requires_tz_aware_timestamp(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    row = _row("a", "hyd-1", "2026-07-01T10:00:00")  # no offset
    with pytest.raises(ValueError, match="timezone"):
        append_rows(path, "hyd-1", [row])


def test_sanity_check_reports_corruption_without_raising(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    append_rows(path, "hyd-1", [_row("a", "hyd-1", "2026-07-01T10:00:00-04:00")])
    with path.open("a", encoding="utf-8") as f:
        f.write("{not valid json\n")
        f.write(json.dumps({"unit_id": "b", "run_id": None}) + "\n")

    report = sanity_check(path)
    assert report["total_rows"] == 3
    assert len(report["parse_errors"]) == 1
    assert report["missing_run_id"] == [3]
    assert report["ok"] is False


def test_sanity_check_clean_ledger(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    append_rows(path, "hyd-1", [_row("a", "hyd-1", "2026-07-01T10:00:00-04:00")])
    report = sanity_check(path)
    assert report["ok"] is True
    assert report["parse_errors"] == []
    assert report["missing_run_id"] == []


# --------------------------------------------------------------------------
# diff_last_two
# --------------------------------------------------------------------------


def test_diff_raises_on_zero_runs(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    with pytest.raises(InsufficientHistoryError, match=r"insufficient history: 0 run\(s\) recorded, 2 required"):
        diff_last_two(path)


def test_diff_raises_on_one_run(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    append_rows(path, "hyd-1", [_row("a", "hyd-1", "2026-07-01T10:00:00-04:00")])
    with pytest.raises(InsufficientHistoryError, match=r"insufficient history: 1 run\(s\) recorded, 2 required"):
        diff_last_two(path)


def test_diff_never_returns_empty_dict_on_insufficient_history(tmp_path):
    # Explicitly assert the failure mode this ticket fixes: no silent {}.
    path = tmp_path / "snapshots.jsonl"
    append_rows(path, "hyd-1", [_row("a", "hyd-1", "2026-07-01T10:00:00-04:00")])
    try:
        diff_last_two(path)
        pytest.fail("expected InsufficientHistoryError, got a return value instead")
    except InsufficientHistoryError:
        pass


def test_diff_classifies_new_removed_price_changed_and_ignores_unchanged(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    append_rows(
        path,
        "hyd-1",
        [
            _row("stays", "hyd-1", "2026-07-01T10:00:00-04:00", price=2500),
            _row("leaves", "hyd-1", "2026-07-01T10:00:01-04:00", price=2600),
            _row("repriced", "hyd-1", "2026-07-01T10:00:02-04:00", price=2600),
        ],
    )
    append_rows(
        path,
        "hyd-2",
        [
            _row("stays", "hyd-2", "2026-07-08T10:00:00-04:00", price=2500),
            _row("repriced", "hyd-2", "2026-07-08T10:00:02-04:00", price=2450),
            _row("arrives", "hyd-2", "2026-07-08T10:00:03-04:00", price=3000),
        ],
    )

    diff = diff_last_two(path)
    assert diff["run_from"] == "hyd-1"
    assert diff["run_to"] == "hyd-2"

    assert [u["unit_id"] for u in diff["new"]] == ["arrives"]
    assert [u["unit_id"] for u in diff["removed"]] == ["leaves"]
    assert len(diff["price_changed"]) == 1
    pc = diff["price_changed"][0]
    assert pc["unit_id"] == "repriced"
    assert pc["from"] == 2600
    assert pc["to"] == 2450
    assert pc["delta"] == -150
    assert pc["pct"] == pytest.approx(-5.77, abs=0.01)

    # "stays" is unchanged (same price both runs) -> no entry anywhere
    all_ids = (
        {u["unit_id"] for u in diff["new"]}
        | {u["unit_id"] for u in diff["removed"]}
        | {c["unit_id"] for c in diff["price_changed"]}
        | {c["unit_id"] for c in diff["price_unknown"]}
    )
    assert "stays" not in all_ids


def test_diff_null_prices_go_to_price_unknown_never_price_changed(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    append_rows(
        path,
        "hyd-1",
        [
            _row("mystery", "hyd-1", "2026-07-01T10:00:00-04:00", price=2500),
            _row("always-null", "hyd-1", "2026-07-01T10:00:01-04:00", price=None, status="recheck"),
        ],
    )
    append_rows(
        path,
        "hyd-2",
        [
            _row("mystery", "hyd-2", "2026-07-08T10:00:00-04:00", price=None, status="recheck"),
            _row("always-null", "hyd-2", "2026-07-08T10:00:01-04:00", price=None, status="recheck"),
        ],
    )
    diff = diff_last_two(path)
    assert diff["price_changed"] == []
    unknown_ids = {u["unit_id"] for u in diff["price_unknown"]}
    assert unknown_ids == {"mystery", "always-null"}
    mystery = next(u for u in diff["price_unknown"] if u["unit_id"] == "mystery")
    assert mystery["from"] == 2500
    assert mystery["to"] is None


def test_diff_no_network_activity_is_pure_local(tmp_path, monkeypatch):
    # Guard against accidental network use: block socket creation entirely.
    import socket

    def _no_network(*args, **kwargs):
        raise AssertionError("diff_last_two must not touch the network")

    monkeypatch.setattr(socket, "socket", _no_network)

    path = tmp_path / "snapshots.jsonl"
    append_rows(path, "hyd-1", [_row("a", "hyd-1", "2026-07-01T10:00:00-04:00")])
    append_rows(path, "hyd-2", [_row("a", "hyd-2", "2026-07-08T10:00:00-04:00")])
    diff_last_two(path)  # must not raise from the monkeypatch


# --------------------------------------------------------------------------
# detect_drops
# --------------------------------------------------------------------------


def _drop_fixture(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    # u1: 1000 -> 900 over exactly 14 days = 10% drop
    append_rows(path, "hyd-1", [_row("u1", "hyd-1", "2026-07-01T00:00:00+00:00", price=1000)])
    append_rows(path, "hyd-2", [_row("u1", "hyd-2", "2026-07-15T00:00:00+00:00", price=900)])
    return path


def test_detect_drops_flags_at_exact_threshold_boundary(tmp_path):
    path = _drop_fixture(tmp_path)
    result = detect_drops(path, min_pct=10.0, window_days=14)
    assert result["insufficient_history"] == 0
    assert len(result["drops"]) == 1
    d = result["drops"][0]
    assert d["unit_id"] == "u1"
    assert d["from"] == 1000
    assert d["to"] == 900
    assert d["pct"] == 10.0
    assert d["window_days"] == 14
    assert d["observed_days"] == 14
    assert d["evidence"]["from_run_id"] == "hyd-1"
    assert d["evidence"]["to_run_id"] == "hyd-2"


def test_detect_drops_excludes_gap_outside_window(tmp_path):
    path = _drop_fixture(tmp_path)
    result = detect_drops(path, min_pct=10.0, window_days=13)  # gap is 14 days > 13
    assert result["drops"] == []


def test_detect_drops_excludes_below_percent_threshold(tmp_path):
    path = _drop_fixture(tmp_path)
    result = detect_drops(path, min_pct=10.01, window_days=14)  # actual drop is exactly 10.0%
    assert result["drops"] == []


def test_detect_drops_excludes_null_prices_and_single_observation_units(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    # single observation only
    append_rows(path, "hyd-1", [_row("single", "hyd-1", "2026-07-01T00:00:00+00:00", price=1000)])
    # never priced across two runs
    append_rows(path, "hyd-1", [_row("nullprice", "hyd-1", "2026-07-01T00:00:01+00:00", price=None, status="recheck")], )
    append_rows(path, "hyd-2", [_row("nullprice", "hyd-2", "2026-07-08T00:00:00+00:00", price=None, status="recheck")])

    result = detect_drops(path, min_pct=1.0, window_days=30)
    assert result["drops"] == []
    # both "single" (1 priced obs) and "nullprice" (0 priced obs) are insufficient history
    assert result["insufficient_history"] == 2


def test_detect_drops_config_driven_thresholds_change_behavior_no_code_change(tmp_path):
    path = _drop_fixture(tmp_path)
    strict = detect_drops(path, min_pct=DEFAULT_DROP_PCT, window_days=DEFAULT_DROP_WINDOW_DAYS)
    loose = detect_drops(path, min_pct=1.0, window_days=30)
    tight_window = detect_drops(path, min_pct=1.0, window_days=1)
    assert len(strict["drops"]) == 1
    assert len(loose["drops"]) == 1
    assert len(tight_window["drops"]) == 0  # same data, different config -> different result


def test_detect_drops_rejects_nonpositive_window(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    with pytest.raises(ValueError, match="window_days must be positive"):
        detect_drops(path, min_pct=5.0, window_days=0)


# --------------------------------------------------------------------------
# trajectory
# --------------------------------------------------------------------------


def test_trajectory_orders_by_observed_at_and_includes_gone_status(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    append_rows(path, "hyd-2", [_row("u1", "hyd-2", "2026-07-08T00:00:00+00:00", price=2450, status="live")])
    append_rows(path, "hyd-1", [_row("u1", "hyd-1", "2026-07-01T00:00:00+00:00", price=2500, status="live")])
    append_rows(path, "hyd-3", [_row("u1", "hyd-3", "2026-07-15T00:00:00+00:00", price=None, status="gone")])

    series = trajectory(path, "u1")
    assert [p["run_id"] for p in series] == ["hyd-1", "hyd-2", "hyd-3"]
    assert series[0]["price"] == 2500
    assert series[-1]["status"] == "gone"
    assert series[-1]["price"] is None


def test_trajectory_unknown_unit_is_empty(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    append_rows(path, "hyd-1", [_row("u1", "hyd-1", "2026-07-01T00:00:00+00:00")])
    assert trajectory(path, "does-not-exist") == []


# --------------------------------------------------------------------------
# digest_markdown
# --------------------------------------------------------------------------


def test_digest_renders_insufficient_history_never_empty(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    text = digest_markdown(path)
    assert "insufficient history: 0 run(s) recorded, 2 required" in text
    assert "## What changed this week" in text


def test_digest_renders_no_changes_line_naming_both_runs(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    append_rows(path, "hyd-1", [_row("a", "hyd-1", "2026-07-01T00:00:00+00:00", price=2500)])
    append_rows(path, "hyd-2", [_row("a", "hyd-2", "2026-07-08T00:00:00+00:00", price=2500)])
    text = digest_markdown(path)
    assert "No changes since last run" in text
    assert "hyd-1" in text and "hyd-2" in text


def test_digest_full_scenario_with_new_removed_priced_and_drop(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    append_rows(
        path,
        "hyd-1",
        [
            _row("stays", "hyd-1", "2026-07-01T00:00:00+00:00", price=2500),
            _row("leaves", "hyd-1", "2026-07-01T00:00:01+00:00", price=2600),
            _row("dropper", "hyd-1", "2026-07-01T00:00:02+00:00", price=1000),
            _row("mystery", "hyd-1", "2026-07-01T00:00:03+00:00", price=2000),
        ],
    )
    append_rows(
        path,
        "hyd-2",
        [
            _row("stays", "hyd-2", "2026-07-08T00:00:00+00:00", price=2500),
            _row("arrives", "hyd-2", "2026-07-08T00:00:01+00:00", price=3100, availability=None),
            _row("dropper", "hyd-2", "2026-07-08T00:00:02+00:00", price=900),
            _row("mystery", "hyd-2", "2026-07-08T00:00:03+00:00", price=None, status="recheck"),
        ],
    )
    text = digest_markdown(path, drop_pct=5.0, drop_window_days=14)

    assert "### New (1)" in text
    assert "arrives" in text
    assert "### Removed (1)" in text
    assert "leaves" in text
    assert "### Price changed (1)" in text
    assert "dropper" in text
    assert "### Price unknown (1)" in text
    assert "mystery" in text
    assert "### Price drops" in text
    # dropper fell 10% within 7 days, well past the 5%/14-day config
    assert "dropper" in text.split("### Price drops")[1]


def test_digest_never_raises(tmp_path):
    path = tmp_path / "does-not-exist.jsonl"
    text = digest_markdown(path)
    assert isinstance(text, str)
    assert text  # non-empty


# --------------------------------------------------------------------------
# heartbeat: append_runlog + heartbeat_overdue
# --------------------------------------------------------------------------


def _stats(units_checked=10, live=8, gone=1, recheck=1, changes=0):
    return {"units_checked": units_checked, "live": live, "gone": gone, "recheck": recheck, "changes": changes}


def test_append_runlog_writes_zero_change_row(tmp_path):
    path = tmp_path / "run-log.jsonl"
    row = append_runlog(path, "hyd-1", _stats(changes=0), at="2026-07-01T09:00:00+00:00")
    assert row["run_id"] == "hyd-1"
    assert row["changes"] == 0
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    stored = json.loads(lines[0])
    assert stored == row


def test_append_runlog_appends_every_call_including_repeated_zero_change(tmp_path):
    path = tmp_path / "run-log.jsonl"
    append_runlog(path, "hyd-1", _stats(changes=0), at="2026-07-01T09:00:00+00:00")
    append_runlog(path, "hyd-2", _stats(changes=0), at="2026-07-08T09:00:00+00:00")
    lines = path.read_text().splitlines()
    assert len(lines) == 2


@pytest.mark.parametrize(
    "bad_stats",
    [
        {"units_checked": 10, "live": 8, "gone": 1, "recheck": 1},  # missing "changes"
        {"units_checked": -1, "live": 8, "gone": 1, "recheck": 1, "changes": 0},
        {"units_checked": 10, "live": 8, "gone": 1, "recheck": 1, "changes": True},
        {"units_checked": "10", "live": 8, "gone": 1, "recheck": 1, "changes": 0},
    ],
)
def test_append_runlog_rejects_bad_stats(tmp_path, bad_stats):
    path = tmp_path / "run-log.jsonl"
    with pytest.raises(ValueError):
        append_runlog(path, "hyd-1", bad_stats)


def test_heartbeat_overdue_true_when_no_file(tmp_path):
    path = tmp_path / "run-log.jsonl"
    assert heartbeat_overdue(path, max_age_days=7) is True


def test_heartbeat_overdue_false_when_recent(tmp_path):
    path = tmp_path / "run-log.jsonl"
    append_runlog(path, "hyd-1", _stats(), at="2026-07-01T09:00:00+00:00")
    assert heartbeat_overdue(path, max_age_days=7, now="2026-07-05T09:00:00+00:00") is False


def test_heartbeat_overdue_true_when_stale(tmp_path):
    path = tmp_path / "run-log.jsonl"
    append_runlog(path, "hyd-1", _stats(), at="2026-07-01T09:00:00+00:00")
    assert heartbeat_overdue(path, max_age_days=7, now="2026-07-10T09:00:00+00:00") is True


# --------------------------------------------------------------------------
# CLI smoke tests
# --------------------------------------------------------------------------


def _run_cli(args, stdin_payload=None, cwd=None):
    input_text = json.dumps(stdin_payload) if stdin_payload is not None else ""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=input_text,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=30,
    )


def test_cli_append_diff_drops_digest_runlog_check(tmp_path):
    snap_path = tmp_path / "snapshots.jsonl"
    runlog_path = tmp_path / "run-log.jsonl"

    rows1 = [dict(_row("u1", "hyd-1", "2026-07-01T00:00:00+00:00", price=1000))]
    r = _run_cli(["append", "--run-id", "hyd-1", "--path", str(snap_path)], stdin_payload=rows1)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["appended"] == 1

    # insufficient history at this point
    r = _run_cli(["diff", "--path", str(snap_path)])
    assert r.returncode == 1
    assert "insufficient history" in r.stderr

    rows2 = [dict(_row("u1", "hyd-2", "2026-07-15T00:00:00+00:00", price=900))]
    r = _run_cli(["append", "--run-id", "hyd-2", "--path", str(snap_path)], stdin_payload=rows2)
    assert r.returncode == 0, r.stderr

    r = _run_cli(["diff", "--path", str(snap_path)])
    assert r.returncode == 0, r.stderr
    diff = json.loads(r.stdout)
    assert diff["run_from"] == "hyd-1"
    assert diff["run_to"] == "hyd-2"

    r = _run_cli(["drops", "--path", str(snap_path), "--min-pct", "5", "--window-days", "30"])
    assert r.returncode == 0, r.stderr
    drops = json.loads(r.stdout)
    assert len(drops["drops"]) == 1

    r = _run_cli(["digest", "--path", str(snap_path)])
    assert r.returncode == 0, r.stderr
    assert "## What changed this week" in r.stdout

    # Pin the heartbeat timestamp so the two --now checks below are
    # reproducible regardless of the real clock.
    stats = _stats(changes=1)
    r = _run_cli(
        ["append", "--run-id", "hyd-2", "--target", "runlog", "--path", str(runlog_path),
         "--at", "2026-07-15T12:00:00+00:00"],
        stdin_payload=stats,
    )
    assert r.returncode == 0, r.stderr

    r = _run_cli(["runlog-check", "--path", str(runlog_path), "--max-age-days", "7", "--now", "2026-07-16T00:00:00+00:00"])
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["overdue"] is False

    r = _run_cli(["runlog-check", "--path", str(runlog_path), "--max-age-days", "1", "--now", "2026-07-20T00:00:00+00:00"])
    assert r.returncode == 1
    assert json.loads(r.stdout)["overdue"] is True


def test_cli_append_rejects_non_array_for_snapshots_target(tmp_path):
    snap_path = tmp_path / "snapshots.jsonl"
    r = _run_cli(["append", "--run-id", "hyd-1", "--path", str(snap_path)], stdin_payload={"not": "a list"})
    assert r.returncode != 0
    assert not snap_path.exists()
