"""Tests for lease_dates.py - pure lease critical-date arithmetic.

Imported via the scripts-path hook in tests/conftest.py. No network calls;
every case is fixture data constructed inline.
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
from pathlib import Path

import pytest

from lease_dates import derive_dates, main

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / ".claude" / "skills" / "apartmentops" / "scripts" / "lease_dates.py"
)


def _labels(result: dict) -> set[str]:
    return {row["label"] for row in result["dates"]}


def _skipped_labels(result: dict) -> set[str]:
    return {row["label"] for row in result["skipped"]}


# --------------------------------------------------------------------------
# Full happy path
# --------------------------------------------------------------------------


def test_all_four_dates_computed_with_full_fields():
    fields = {
        "lease_start": "2026-08-01",
        "lease_end": "2027-07-31",
        "renewal_notice_days": 60,
        "concession": {"free_months": 2, "position": "front"},
        "increase_notice_days": 30,
        "deposit_return_days": 30,
    }
    result = derive_dates(fields, today=datetime.date(2026, 7, 15))

    assert _labels(result) == {
        "Renewal notice deadline",
        "Concession reversion date",
        "Rent-increase notice deadline",
        "Deposit-return deadline",
    }
    assert result["skipped"] == []

    by_label = {row["label"]: row for row in result["dates"]}

    assert by_label["Renewal notice deadline"]["date"] == "2027-06-01"
    assert "lease_end (2027-07-31)" in by_label["Renewal notice deadline"]["computed_from"]
    assert "60" in by_label["Renewal notice deadline"]["computed_from"]

    assert by_label["Concession reversion date"]["date"] == "2026-10-01"
    assert "lease_start (2026-08-01)" in by_label["Concession reversion date"]["computed_from"]

    assert by_label["Rent-increase notice deadline"]["date"] == "2027-07-01"

    assert by_label["Deposit-return deadline"]["date"] == "2027-08-30"

    # dates are returned in chronological order
    iso_dates = [row["date"] for row in result["dates"]]
    assert iso_dates == sorted(iso_dates)


def test_dates_are_iso_strings_and_no_extraneous_keys():
    fields = {"lease_end": "2027-07-31", "deposit_return_days": 30}
    result = derive_dates(fields, today=datetime.date(2026, 7, 15))
    assert len(result["dates"]) == 1
    row = result["dates"][0]
    assert set(row.keys()) == {"date", "label", "computed_from", "status"}
    datetime.date.fromisoformat(row["date"])  # does not raise


# --------------------------------------------------------------------------
# Missing inputs -> skipped, never defaulted
# --------------------------------------------------------------------------


def test_empty_fields_skips_everything():
    result = derive_dates({}, today=datetime.date(2026, 7, 15))
    assert result["dates"] == []
    assert _skipped_labels(result) == {
        "Renewal notice deadline",
        "Concession reversion date",
        "Rent-increase notice deadline",
        "Deposit-return deadline",
    }
    for row in result["skipped"]:
        assert row["missing"]  # every skip states a reason


def test_missing_renewal_notice_days_only_skips_that_date():
    fields = {
        "lease_start": "2026-08-01",
        "lease_end": "2027-07-31",
        "concession": {"free_months": 1, "position": "front"},
        "increase_notice_days": 30,
        "deposit_return_days": 30,
    }
    result = derive_dates(fields, today=datetime.date(2026, 7, 15))
    assert "Renewal notice deadline" in _skipped_labels(result)
    assert "Renewal notice deadline" not in _labels(result)
    skip_row = next(r for r in result["skipped"] if r["label"] == "Renewal notice deadline")
    assert "renewal_notice_days" in skip_row["missing"]
    # the other three still compute
    assert _labels(result) == {
        "Concession reversion date",
        "Rent-increase notice deadline",
        "Deposit-return deadline",
    }


def test_missing_lease_end_skips_the_three_lease_end_dates_but_not_concession():
    fields = {
        "lease_start": "2026-08-01",
        "concession": {"free_months": 2, "position": "front"},
        "renewal_notice_days": 60,
        "increase_notice_days": 30,
        "deposit_return_days": 30,
    }
    result = derive_dates(fields, today=datetime.date(2026, 7, 15))
    assert _labels(result) == {"Concession reversion date"}
    assert _skipped_labels(result) == {
        "Renewal notice deadline",
        "Rent-increase notice deadline",
        "Deposit-return deadline",
    }
    for row in result["skipped"]:
        assert "lease_end" in row["missing"]


def test_unparseable_date_is_skipped_not_crashed():
    fields = {"lease_end": "not-a-date", "deposit_return_days": 30}
    result = derive_dates(fields, today=datetime.date(2026, 7, 15))
    assert result["dates"] == []
    skip_row = next(r for r in result["skipped"] if r["label"] == "Deposit-return deadline")
    assert "unparseable" in skip_row["missing"]


def test_unparseable_notice_days_is_skipped_not_crashed():
    fields = {"lease_end": "2027-07-31", "deposit_return_days": "thirty"}
    result = derive_dates(fields, today=datetime.date(2026, 7, 15))
    assert result["dates"] == []
    skip_row = next(r for r in result["skipped"] if r["label"] == "Deposit-return deadline")
    assert "deposit_return_days" in skip_row["missing"]
    assert "unparseable" in skip_row["missing"]


# --------------------------------------------------------------------------
# Concession position: front vs spread
# --------------------------------------------------------------------------


def test_spread_concession_never_yields_a_reversion_date():
    fields = {
        "lease_start": "2026-08-01",
        "concession": {"free_months": 1, "position": "spread"},
    }
    result = derive_dates(fields, today=datetime.date(2026, 7, 15))
    assert "Concession reversion date" not in _labels(result)
    skip_row = next(r for r in result["skipped"] if r["label"] == "Concession reversion date")
    assert "spread" in skip_row["missing"]


def test_concession_missing_position_is_skipped():
    fields = {"lease_start": "2026-08-01", "concession": {"free_months": 2}}
    result = derive_dates(fields, today=datetime.date(2026, 7, 15))
    assert "Concession reversion date" not in _labels(result)
    skip_row = next(r for r in result["skipped"] if r["label"] == "Concession reversion date")
    assert "concession.position" in skip_row["missing"]


def test_concession_wrong_shape_is_skipped_not_crashed():
    fields = {"lease_start": "2026-08-01", "concession": "two months free"}
    result = derive_dates(fields, today=datetime.date(2026, 7, 15))
    assert "Concession reversion date" not in _labels(result)


def test_concession_reversion_month_end_overflow_clamps():
    # Jan 31 + 1 month -> Feb 28 (2026 is not a leap year)
    fields = {"lease_start": "2026-01-31", "concession": {"free_months": 1, "position": "front"}}
    result = derive_dates(fields, today=datetime.date(2026, 1, 1))
    row = next(r for r in result["dates"] if r["label"] == "Concession reversion date")
    assert row["date"] == "2026-02-28"


def test_concession_reversion_month_end_overflow_leap_year():
    fields = {"lease_start": "2024-01-31", "concession": {"free_months": 1, "position": "front"}}
    result = derive_dates(fields, today=datetime.date(2024, 1, 1))
    row = next(r for r in result["dates"] if r["label"] == "Concession reversion date")
    assert row["date"] == "2024-02-29"


# --------------------------------------------------------------------------
# status: upcoming vs past_due, driven by injectable today
# --------------------------------------------------------------------------


def test_status_past_due_when_today_after_deadline():
    fields = {"lease_end": "2027-07-31", "deposit_return_days": 30}
    result = derive_dates(fields, today=datetime.date(2027, 9, 1))
    row = result["dates"][0]
    assert row["date"] == "2027-08-30"
    assert row["status"] == "past_due"


def test_status_upcoming_when_today_before_deadline():
    fields = {"lease_end": "2027-07-31", "deposit_return_days": 30}
    result = derive_dates(fields, today=datetime.date(2026, 1, 1))
    row = result["dates"][0]
    assert row["status"] == "upcoming"


def test_status_upcoming_on_the_day_of_the_deadline():
    fields = {"lease_end": "2027-07-31", "deposit_return_days": 30}
    result = derive_dates(fields, today=datetime.date(2027, 8, 30))
    row = result["dates"][0]
    assert row["status"] == "upcoming"


def test_default_today_is_real_today_when_omitted():
    fields = {"lease_end": "2099-01-01", "deposit_return_days": 1}
    result = derive_dates(fields)  # no today supplied
    row = result["dates"][0]
    assert row["status"] == "upcoming"  # far future relative to real "today"


# --------------------------------------------------------------------------
# Type errors and edge inputs
# --------------------------------------------------------------------------


def test_non_dict_fields_raises_type_error():
    with pytest.raises(TypeError):
        derive_dates(["not", "a", "dict"])  # type: ignore[arg-type]


def test_zero_day_notice_period_is_valid_not_treated_as_missing():
    fields = {"lease_end": "2027-07-31", "renewal_notice_days": 0}
    result = derive_dates(fields, today=datetime.date(2026, 1, 1))
    row = next(r for r in result["dates"] if r["label"] == "Renewal notice deadline")
    assert row["date"] == "2027-07-31"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_reads_fields_json_and_prints_result(tmp_path):
    fields_path = tmp_path / "fields.json"
    fields_path.write_text(
        json.dumps({"lease_end": "2027-07-31", "deposit_return_days": 30})
    )
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(fields_path), "--today", "2026-01-01"],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(proc.stdout)
    assert payload["dates"][0]["date"] == "2027-08-30"
    assert payload["dates"][0]["status"] == "upcoming"


def test_cli_missing_file_reports_error_and_nonzero_exit(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "nope.json")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "no such file" in proc.stderr.lower()


def test_cli_invalid_json_reports_error_and_nonzero_exit(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(bad)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "invalid json" in proc.stderr.lower()


def test_cli_no_args_prints_usage_and_exits_2():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "usage" in proc.stderr.lower()


def test_main_function_returns_zero_on_success(tmp_path, capsys):
    fields_path = tmp_path / "fields.json"
    fields_path.write_text(json.dumps({"lease_end": "2027-07-31", "deposit_return_days": 30}))
    # main() reads sys.argv directly; exercise it through argv patching.
    old_argv = sys.argv
    try:
        sys.argv = ["lease_dates.py", str(fields_path)]
        assert main() == 0
    finally:
        sys.argv = old_argv
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["dates"][0]["date"] == "2027-08-30"
