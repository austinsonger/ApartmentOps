#!/usr/bin/env python3
"""Pure date math for lease critical dates - no network, no guessing.

Turns a small set of lease facts (already extracted and cited elsewhere -
see references/lease-fields.md and the apartmentops-lease skill) into a
flat list of dated events: the renewal notice deadline, the concession
reversion date, the rent-increase notice deadline, and the deposit-return
deadline. Every date is computed only from inputs that are actually
present; a missing input means the date is omitted and reported in
"skipped" instead of being defaulted or guessed - see derive_dates().

Jurisdiction-sourced values (statutory notice periods, local ordinances)
are NOT looked up here. This module does arithmetic only; the caller
(the apartmentops-lease skill, during its session) is responsible for
researching and citing any statutory figure before passing it in as e.g.
renewal_notice_days. If that research was not done, simply omit the
field - it will show up in "skipped" rather than being invented.

Usage:
    python3 lease_dates.py fields.json
    python3 lease_dates.py fields.json --today 2026-07-15

fields.json is a JSON object with any subset of these keys (see
derive_dates() docstring for the full field list and semantics):

    {
      "lease_start": "2026-08-01",
      "lease_end": "2027-07-31",
      "renewal_notice_days": 60,
      "concession": {"free_months": 2, "position": "front"},
      "increase_notice_days": 30,
      "deposit_return_days": 30
    }

Output (to stdout) is a JSON object:

    {
      "dates": [{"date": "...", "label": "...", "computed_from": "...",
                 "status": "upcoming" | "past_due"}, ...],
      "skipped": [{"label": "...", "missing": "..."}, ...]
    }

Notes learned the hard way:
- "today" is injectable so tests (and any caller that wants a stable
  re-render) do not depend on the wall clock; it only affects the
  upcoming/past_due status tag, never which dates get computed.
- Concession reversion is only derived for a front-loaded concession
  (free months at the start of the term). A "spread" concession (the
  discount is baked into every month's rent) has no single reversion
  date to compute, so it is always reported in "skipped", never
  approximated.
- Month-arithmetic for the reversion date clamps day-of-month overflow
  (e.g. lease_start Jan 31 + 1 month -> Feb 28/29), the same rule most
  calendaring software uses.
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from typing import Any


def _parse_date(value: Any) -> datetime.date | None:
    """Best-effort parse of an ISO date (or datetime) string. None on failure."""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.date.fromisoformat(text[:10])
    except ValueError:
        pass
    try:
        return datetime.datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _parse_int(value: Any) -> int | None:
    """Best-effort parse of an integer day-count. None on failure."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        next_month = datetime.date(year + 1, 1, 1)
    else:
        next_month = datetime.date(year, month + 1, 1)
    return (next_month - datetime.date(year, month, 1)).days


def _add_months(start: datetime.date, months: int) -> datetime.date:
    """Add a whole number of months, clamping day-of-month overflow."""
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, _days_in_month(year, month))
    return datetime.date(year, month, day)


def derive_dates(fields: dict[str, Any], today: datetime.date | None = None) -> dict[str, list]:
    """Derive lease critical dates from a flat fields dict. Pure, no I/O.

    fields (all optional - only supplied inputs are used, nothing is
    defaulted when a key is absent):

        lease_start           ISO date string, e.g. "2026-08-01"
        lease_end             ISO date string, e.g. "2027-07-31"
        renewal_notice_days   int; days of notice required before lease_end.
                               Jurisdiction-sourced values must already be
                               cited by the caller - this function does not
                               look anything up.
        concession             {"free_months": int,
                                 "position": "front" | "spread"}
                               Only "front" yields a reversion date; a
                               "spread" concession has no single reversion
                               date and is always skipped.
        increase_notice_days  int; days of notice required before a rent
                               increase takes effect (also jurisdiction- or
                               lease-sourced, cited by the caller).
        deposit_return_days   int; days after lease_end the security
                               deposit is due back.

    today: reference date used only to tag each derived date "upcoming" or
        "past_due". Defaults to the real current date; pass an explicit
        date for deterministic output (tests, reproducible re-renders).

    Returns:
        {
          "dates": [
            {"date": "YYYY-MM-DD", "label": str, "computed_from": str,
             "status": "upcoming" | "past_due"},
            ...
          ],
          "skipped": [{"label": str, "missing": str}, ...]
        }

    A date is included in "dates" only when every input it depends on is
    present and parses cleanly. Otherwise it lands in "skipped" with a
    human-readable reason naming exactly which input(s) were absent or
    unparseable - the date itself is never guessed, interpolated, or
    defaulted.
    """
    if not isinstance(fields, dict):
        raise TypeError("fields must be a dict")

    if today is None:
        today = datetime.date.today()

    dates: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []

    def emit(label: str, value: datetime.date, computed_from: str) -> None:
        dates.append(
            {
                "date": value.isoformat(),
                "label": label,
                "computed_from": computed_from,
                "status": "past_due" if value < today else "upcoming",
            }
        )

    def skip(label: str, missing: str) -> None:
        skipped.append({"label": label, "missing": missing})

    lease_start_raw = fields.get("lease_start")
    lease_end_raw = fields.get("lease_end")
    lease_start = _parse_date(lease_start_raw)
    lease_end = _parse_date(lease_end_raw)

    # -- Renewal notice deadline = lease_end - renewal_notice_days --------
    label = "Renewal notice deadline"
    renewal_notice_days = _parse_int(fields.get("renewal_notice_days"))
    missing_bits: list[str] = []
    if lease_end_raw is None:
        missing_bits.append("lease_end")
    elif lease_end is None:
        missing_bits.append("lease_end (unparseable)")
    if fields.get("renewal_notice_days") is None:
        missing_bits.append("renewal_notice_days")
    elif renewal_notice_days is None:
        missing_bits.append("renewal_notice_days (unparseable)")
    if missing_bits:
        skip(label, ", ".join(missing_bits))
    else:
        assert lease_end is not None and renewal_notice_days is not None
        deadline = lease_end - datetime.timedelta(days=renewal_notice_days)
        emit(
            label,
            deadline,
            f"lease_end ({lease_end.isoformat()}) minus renewal_notice_days "
            f"({renewal_notice_days}) days",
        )

    # -- Concession reversion date (front-loaded only) ---------------------
    label = "Concession reversion date"
    concession = fields.get("concession")
    if concession is None:
        skip(label, "concession")
    elif not isinstance(concession, dict):
        skip(label, "concession (invalid shape, expected an object)")
    else:
        position = concession.get("position")
        free_months = _parse_int(concession.get("free_months"))
        missing_bits = []
        if lease_start_raw is None:
            missing_bits.append("lease_start")
        elif lease_start is None:
            missing_bits.append("lease_start (unparseable)")
        if concession.get("free_months") is None:
            missing_bits.append("concession.free_months")
        elif free_months is None:
            missing_bits.append("concession.free_months (unparseable)")
        if position is None:
            missing_bits.append("concession.position")
        if missing_bits:
            skip(label, ", ".join(missing_bits))
        elif position != "front":
            skip(
                label,
                f"concession.position is '{position}', not 'front' - a spread "
                "concession has no single reversion date to derive",
            )
        else:
            assert lease_start is not None and free_months is not None
            reversion = _add_months(lease_start, free_months)
            emit(
                label,
                reversion,
                f"lease_start ({lease_start.isoformat()}) plus "
                f"concession.free_months ({free_months}) months, front-loaded",
            )

    # -- Rent-increase notice deadline = lease_end - increase_notice_days --
    label = "Rent-increase notice deadline"
    increase_notice_days = _parse_int(fields.get("increase_notice_days"))
    missing_bits = []
    if lease_end_raw is None:
        missing_bits.append("lease_end")
    elif lease_end is None:
        missing_bits.append("lease_end (unparseable)")
    if fields.get("increase_notice_days") is None:
        missing_bits.append("increase_notice_days")
    elif increase_notice_days is None:
        missing_bits.append("increase_notice_days (unparseable)")
    if missing_bits:
        skip(label, ", ".join(missing_bits))
    else:
        assert lease_end is not None and increase_notice_days is not None
        deadline = lease_end - datetime.timedelta(days=increase_notice_days)
        emit(
            label,
            deadline,
            f"lease_end ({lease_end.isoformat()}) minus increase_notice_days "
            f"({increase_notice_days}) days",
        )

    # -- Deposit-return deadline = lease_end + deposit_return_days ---------
    label = "Deposit-return deadline"
    deposit_return_days = _parse_int(fields.get("deposit_return_days"))
    missing_bits = []
    if lease_end_raw is None:
        missing_bits.append("lease_end")
    elif lease_end is None:
        missing_bits.append("lease_end (unparseable)")
    if fields.get("deposit_return_days") is None:
        missing_bits.append("deposit_return_days")
    elif deposit_return_days is None:
        missing_bits.append("deposit_return_days (unparseable)")
    if missing_bits:
        skip(label, ", ".join(missing_bits))
    else:
        assert lease_end is not None and deposit_return_days is not None
        deadline = lease_end + datetime.timedelta(days=deposit_return_days)
        emit(
            label,
            deadline,
            f"lease_end ({lease_end.isoformat()}) plus deposit_return_days "
            f"({deposit_return_days}) days",
        )

    dates.sort(key=lambda row: row["date"])
    return {"dates": dates, "skipped": skipped}


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        print("usage: lease_dates.py fields.json [--today YYYY-MM-DD]", file=sys.stderr)
        return 2

    today: datetime.date | None = None
    if "--today" in argv:
        idx = argv.index("--today")
        if idx + 1 >= len(argv):
            print("error: --today requires a YYYY-MM-DD value", file=sys.stderr)
            return 2
        today = _parse_date(argv[idx + 1])
        if today is None:
            print(f"error: could not parse --today value {argv[idx + 1]!r}", file=sys.stderr)
            return 2
        del argv[idx : idx + 2]

    if not argv:
        print("usage: lease_dates.py fields.json [--today YYYY-MM-DD]", file=sys.stderr)
        return 2

    fields_path = Path(argv[0])
    try:
        fields = json.loads(fields_path.read_text())
    except FileNotFoundError:
        print(f"error: no such file: {fields_path}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON in {fields_path}: {exc}", file=sys.stderr)
        return 1

    if not isinstance(fields, dict):
        print(f"error: {fields_path} must contain a JSON object", file=sys.stderr)
        return 1

    result = derive_dates(fields, today=today)
    json.dump(result, sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
