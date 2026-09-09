#!/usr/bin/env python3
"""Append-only market-memory ledger and delta engine for ApartmentOps.

Every hydrate run appends one observation row per unit to a plain,
append-only file (`apartmentops/data/snapshots.jsonl`), stamped with an
explicit run_id chosen at the start of that run. Everything else in this
module is a pure-local, read-only computation over that file: a two-run
delta diff, a windowed price-drop detector, per-unit trajectories, a
rendered weekly digest, and a heartbeat run-log the freshness banner keys
on. No network calls anywhere in this file.

Row schema (one JSON object per line in snapshots.jsonl):
    {"unit_id": "tower2-2207", "url": "https://...",
     "observed_at": "2026-07-13T14:02:11-04:00", "run_id": "hyd-20260713-1402",
     "price": 2450, "availability": "2026-08-01", "status": "live",
     "fetch_evidence": "apartmentops/shots/tower2-2207.png"}

See ../references/ledger.md for the full schema, the run_id convention, the
heartbeat contract, and the loud-error rule this module enforces.

Library usage (import directly - no subprocess needed):
    from snapshots import append_rows, diff_last_two, detect_drops
    append_rows("apartmentops/data/snapshots.jsonl", "hyd-20260713-1402", rows)
    diff_last_two("apartmentops/data/snapshots.jsonl")   # raises below 2 runs

CLI usage:
    python3 snapshots.py append --run-id hyd-20260713-1402 < rows.json
    python3 snapshots.py append --run-id hyd-20260713-1402 --target runlog [--at ISO-8601] < stats.json
    python3 snapshots.py diff [--path FILE]
    python3 snapshots.py drops [--min-pct 5] [--window-days 14] [--path FILE]
    python3 snapshots.py digest [--drop-pct 5] [--window-days 14] [--path FILE]
    python3 snapshots.py runlog-check [--max-age-days 10] [--path FILE]

Notes learned building this:
- Insufficient history (fewer than two run_ids recorded) is a hard error,
  never a silently empty diff - a scheduled reader that gets {} on "not
  enough data yet" cannot tell that apart from a genuinely quiet market.
- A missing heartbeat row is the alarm, not the content of one - append
  the run-log row on every hydrate, including runs with zero changes.
- Nothing here guesses. A field the caller could not confirm goes in as
  null and stays null; this module never carries a value forward from a
  previous run or interpolates between two observations.
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys
from typing import Any, Iterable, Iterator, Mapping, Sequence

DEFAULT_SNAPSHOTS_PATH = "apartmentops/data/snapshots.jsonl"
DEFAULT_RUNLOG_PATH = "apartmentops/data/run-log.jsonl"

# Documented defaults for the config.yml ledger block (ledger.drop_pct,
# ledger.drop_window_days) - see ledger.md. The CLI and library functions
# accept overrides; nothing here reads config.yml itself.
DEFAULT_DROP_PCT = 5.0
DEFAULT_DROP_WINDOW_DAYS = 14
DEFAULT_HEARTBEAT_MAX_AGE_DAYS = 10  # a week plus scheduling slack

VALID_STATUSES = ("live", "gone", "recheck")
ROW_FIELDS = (
    "unit_id", "url", "observed_at", "run_id", "price", "availability",
    "status", "fetch_evidence",
)
RUNLOG_STAT_FIELDS = ("units_checked", "live", "gone", "recheck", "changes")


class LedgerError(Exception):
    """Base class for snapshot-ledger errors."""


class InsufficientHistoryError(LedgerError):
    """Raised when a computation needs >= 2 runs but the ledger has fewer.

    This is a deliberate loud failure. The source pattern this module fixes
    returned a silent empty diff in this situation, which a scheduled
    consumer could not distinguish from "checked, nothing changed". Catch
    this exception to render a message; never treat it as "no changes".
    """


# --------------------------------------------------------------------------
# low-level parsing / IO
# --------------------------------------------------------------------------


def _parse_ts(value: Any, field: str = "observed_at") -> datetime.datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO 8601 string, got {value!r}")
    try:
        ts = datetime.datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} is not valid ISO 8601: {value!r}") from exc
    if ts.tzinfo is None:
        raise ValueError(f"{field} must include a timezone offset: {value!r}")
    return ts


def _coerce_now(value: datetime.datetime | str | None) -> datetime.datetime:
    if value is None:
        return datetime.datetime.now(datetime.timezone.utc)
    if isinstance(value, str):
        return _parse_ts(value, "now")
    if value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value


def _validate_row(row: Mapping[str, Any], run_id: str) -> dict:
    missing = [f for f in ROW_FIELDS if f not in row]
    if missing:
        raise ValueError(f"snapshot row missing required field(s) {missing}: {row!r}")
    out = {f: row[f] for f in ROW_FIELDS}

    if not isinstance(out["unit_id"], str) or not out["unit_id"]:
        raise ValueError(f"unit_id must be a non-empty string: {row!r}")
    if not isinstance(out["url"], str) or not out["url"]:
        raise ValueError(f"url must be a non-empty string: {row!r}")
    _parse_ts(out["observed_at"])
    if not isinstance(out["run_id"], str) or not out["run_id"]:
        raise ValueError(f"run_id must be a non-empty string: {row!r}")
    if out["run_id"] != run_id:
        raise ValueError(
            f"row run_id {out['run_id']!r} does not match the append run_id "
            f"{run_id!r} - every row in one append() call must carry the "
            "same explicit run_id chosen at hydrate start"
        )
    price = out["price"]
    if price is not None and (isinstance(price, bool) or not isinstance(price, (int, float))):
        raise ValueError(f"price must be a number or null, got {price!r}")
    if out["availability"] is not None and not isinstance(out["availability"], str):
        raise ValueError(f"availability must be a string or null, got {out['availability']!r}")
    if out["status"] not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}, got {out['status']!r}")
    if out["fetch_evidence"] is not None and not isinstance(out["fetch_evidence"], str):
        raise ValueError(f"fetch_evidence must be a string or null, got {out['fetch_evidence']!r}")

    # Extra caller-supplied fields ride along after the canonical ones -
    # never dropped, never fabricated.
    for key, val in row.items():
        if key not in ROW_FIELDS:
            out[key] = val
    return out


def _read_rows(path: str | pathlib.Path) -> Iterator[dict]:
    """Yield every row in the ledger. Raises loudly on a malformed line -
    a corrupt ledger should never be silently skipped over."""
    p = pathlib.Path(path)
    if not p.exists():
        return
    with p.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise LedgerError(f"{p}:{lineno}: unparseable JSON line: {exc}") from exc


def _rows(path: str | pathlib.Path) -> list[dict]:
    return list(_read_rows(path))


# --------------------------------------------------------------------------
# ledger writer
# --------------------------------------------------------------------------


def append_rows(path: str | pathlib.Path, run_id: str, rows: Iterable[Mapping[str, Any]]) -> int:
    """Validate and append one row per unit for this hydrate run.

    Strictly append-only: this never rewrites or reorders an existing line.
    Every row is validated before anything is written, so one bad row in a
    batch aborts the whole append rather than partially corrupting the
    ledger. A field the caller could not confirm must be passed as None -
    it is written as null, never guessed and never carried forward from a
    previous run.

    Returns the number of rows appended.
    """
    if not run_id or not isinstance(run_id, str):
        raise ValueError("run_id must be a non-empty string")
    validated = [_validate_row(row, run_id) for row in rows]
    if not validated:
        return 0
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for row in validated:
            f.write(json.dumps(row))
            f.write("\n")
    return len(validated)


def append_runlog(
    path: str | pathlib.Path,
    run_id: str,
    stats: Mapping[str, Any],
    at: datetime.datetime | str | None = None,
) -> dict:
    """Append one heartbeat row to run-log.jsonl - on every hydrate,
    including zero-change runs. Call this unconditionally at the end of
    every hydrate; the absence of a weekly row (not its content) is what
    tells heartbeat_overdue() and the dashboard freshness banner that the
    scheduler died. Skipping this call on a "nothing happened" run defeats
    the whole point.

    stats must supply units_checked, live, gone, recheck, changes as
    non-negative ints. Returns the row actually written.
    """
    if not run_id or not isinstance(run_id, str):
        raise ValueError("run_id must be a non-empty string")
    missing = [f for f in RUNLOG_STAT_FIELDS if f not in stats]
    if missing:
        raise ValueError(f"run-log stats missing required field(s) {missing}: {dict(stats)!r}")

    row: dict[str, Any] = {"run_id": run_id, "at": _coerce_now(at).isoformat(timespec="seconds")}
    for field in RUNLOG_STAT_FIELDS:
        val = stats[field]
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            raise ValueError(f"run-log stat {field!r} must be a non-negative int, got {val!r}")
        row[field] = val

    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row))
        f.write("\n")
    return row


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------


def load_runs(path: str | pathlib.Path) -> list[str]:
    """Every distinct run_id in the ledger, ordered by first-observed
    timestamp ascending (oldest run first). "First-observed" is the
    earliest observed_at among that run_id's own rows.

    Do not assume there are >= 2 runs; diff_last_two() checks that and
    raises InsufficientHistoryError loudly when there are not.
    """
    first_seen: dict[str, datetime.datetime] = {}
    for row in _read_rows(path):
        rid = row.get("run_id")
        if not rid:
            raise LedgerError(f"row without run_id in {path}: {row!r}")
        ts = _parse_ts(row["observed_at"])
        prev = first_seen.get(rid)
        if prev is None or ts < prev:
            first_seen[rid] = ts
    return sorted(first_seen, key=lambda rid: (first_seen[rid], rid))


def rows_for_run(path: str | pathlib.Path, run_id: str) -> list[dict]:
    """All rows stamped with run_id, in ledger (append) order."""
    return [row for row in _read_rows(path) if row.get("run_id") == run_id]


def sanity_check(path: str | pathlib.Path) -> dict:
    """Validate every line in the ledger: does it parse as JSON, does it
    carry a run_id. Feeds the hydrate run report's ledger sanity section
    (ticket: "every row parses and every row has a run_id").

    Unlike the rest of this module, this never raises for a malformed line
    - its whole job is to surface corruption in a report, not crash on it.
    """
    p = pathlib.Path(path)
    total = 0
    parse_errors: list[str] = []
    missing_run_id: list[int] = []
    if p.exists():
        with p.open("r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    parse_errors.append(f"line {lineno}: {exc}")
                    continue
                if not row.get("run_id"):
                    missing_run_id.append(lineno)
    return {
        "path": str(p),
        "total_rows": total,
        "parse_errors": parse_errors,
        "missing_run_id": missing_run_id,
        "ok": not parse_errors and not missing_run_id,
    }


# --------------------------------------------------------------------------
# delta diff
# --------------------------------------------------------------------------


def diff_last_two(path: str | pathlib.Path) -> dict:
    """Classify units as new, removed, or price-changed between the two
    most recent distinct runs in the ledger.

    Raises InsufficientHistoryError when fewer than two distinct run_ids
    are recorded. This NEVER returns an empty diff for missing history -
    an empty diff and "nothing changed" must stay distinguishable to a
    downstream reader.

    Returns:
        {"run_from": run_id, "run_to": run_id,
         "new": [{"unit_id","url","price","status"}, ...],
         "removed": [{"unit_id","url","price","status"}, ...],
         "price_changed": [{"unit_id","url","from","to","delta","pct"}, ...],
         "price_unknown": [{"unit_id","url","from","to"}, ...]}

    "price_unknown" holds units present in both runs where either
    observation's price is null - a null is never treated as zero, and
    never treated as "unchanged".  Units present in both runs with an
    identical non-null price produce no entry anywhere (truly unchanged).
    """
    runs = load_runs(path)
    if len(runs) < 2:
        raise InsufficientHistoryError(
            f"insufficient history: {len(runs)} run(s) recorded, 2 required"
        )
    prev_run, latest_run = runs[-2], runs[-1]

    prev_units: dict[str, dict] = {}
    for row in rows_for_run(path, prev_run):
        prev_units[row["unit_id"]] = row  # last-wins if a run has a duplicate row
    latest_units: dict[str, dict] = {}
    for row in rows_for_run(path, latest_run):
        latest_units[row["unit_id"]] = row

    new_ids = sorted(latest_units.keys() - prev_units.keys())
    removed_ids = sorted(prev_units.keys() - latest_units.keys())
    common_ids = sorted(latest_units.keys() & prev_units.keys())

    price_changed = []
    price_unknown = []
    for uid in common_ids:
        old_price = prev_units[uid]["price"]
        new_price = latest_units[uid]["price"]
        if old_price is None or new_price is None:
            price_unknown.append(
                {"unit_id": uid, "url": latest_units[uid]["url"], "from": old_price, "to": new_price}
            )
            continue
        if old_price != new_price:
            pct = round((new_price - old_price) / old_price * 100, 2) if old_price else None
            price_changed.append(
                {
                    "unit_id": uid,
                    "url": latest_units[uid]["url"],
                    "from": old_price,
                    "to": new_price,
                    "delta": new_price - old_price,
                    "pct": pct,
                }
            )
        # equal, non-null prices: genuinely unchanged, no entry anywhere

    return {
        "run_from": prev_run,
        "run_to": latest_run,
        "new": [
            {
                "unit_id": uid,
                "url": latest_units[uid]["url"],
                "price": latest_units[uid]["price"],
                "status": latest_units[uid]["status"],
            }
            for uid in new_ids
        ],
        "removed": [
            {
                "unit_id": uid,
                "url": prev_units[uid]["url"],
                "price": prev_units[uid]["price"],
                "status": prev_units[uid]["status"],
            }
            for uid in removed_ids
        ],
        "price_changed": price_changed,
        "price_unknown": price_unknown,
    }


# --------------------------------------------------------------------------
# price-drop detector + trajectories
# --------------------------------------------------------------------------


def detect_drops(
    path: str | pathlib.Path,
    min_pct: float,
    window_days: int,
    now: datetime.datetime | str | None = None,
) -> dict:
    """Flag units whose price fell by at least min_pct percent within
    window_days.

    For each unit: take its most recent priced (non-null) observation at
    or before `now` as the current price, then compare it against the
    EARLIEST priced observation still inside the window
    [current.observed_at - window_days, current.observed_at]. That is the
    "before" baseline. A drop is asserted only between those two recorded
    observations - no interpolation, no extrapolation - and a gap larger
    than window_days between them excludes the unit even if the price fell
    a lot, per the design's "no extrapolation beyond them" rule.

    now defaults to the latest observed_at present anywhere in the ledger,
    so the function is reproducible without a wall clock; pass an explicit
    now (e.g. from digest_markdown) to evaluate as of a different instant.

    Units with fewer than two priced observations (at or before now) are
    excluded from drop detection and counted under "insufficient_history"
    rather than silently omitted. Null prices never participate in the
    percent math.

    Returns:
        {"drops": [{"unit_id","url","from","to","pct","window_days",
                     "observed_days","evidence": {"from_run_id",
                     "from_observed_at","to_run_id","to_observed_at"}}, ...],
         "insufficient_history": int, "min_pct": float, "window_days": int}
    """
    if window_days <= 0:
        raise ValueError("window_days must be positive")

    rows = _rows(path)
    if not rows:
        return {"drops": [], "insufficient_history": 0, "min_pct": min_pct, "window_days": window_days}

    now_ts = _coerce_now(now) if now is not None else max(_parse_ts(r["observed_at"]) for r in rows)

    by_unit: dict[str, list[dict]] = {}
    for row in rows:
        by_unit.setdefault(row["unit_id"], []).append(row)

    drops = []
    insufficient = 0
    for unit_id, unit_rows in by_unit.items():
        priced = sorted(
            (r for r in unit_rows if r["price"] is not None and _parse_ts(r["observed_at"]) <= now_ts),
            key=lambda r: _parse_ts(r["observed_at"]),
        )
        if len(priced) < 2:
            insufficient += 1
            continue

        latest = priced[-1]
        latest_ts = _parse_ts(latest["observed_at"])
        window_start = latest_ts - datetime.timedelta(days=window_days)
        candidates = [r for r in priced[:-1] if _parse_ts(r["observed_at"]) >= window_start]
        if not candidates:
            continue

        baseline = min(candidates, key=lambda r: _parse_ts(r["observed_at"]))
        if not baseline["price"]:  # zero or falsy baseline price: percent is undefined
            continue
        pct = (baseline["price"] - latest["price"]) / baseline["price"] * 100
        if pct >= min_pct:
            baseline_ts = _parse_ts(baseline["observed_at"])
            drops.append(
                {
                    "unit_id": unit_id,
                    "url": latest["url"],
                    "from": baseline["price"],
                    "to": latest["price"],
                    "pct": round(pct, 2),
                    "window_days": window_days,
                    "observed_days": (latest_ts - baseline_ts).days,
                    "evidence": {
                        "from_run_id": baseline["run_id"],
                        "from_observed_at": baseline["observed_at"],
                        "to_run_id": latest["run_id"],
                        "to_observed_at": latest["observed_at"],
                    },
                }
            )

    drops.sort(key=lambda d: d["pct"], reverse=True)
    return {"drops": drops, "insufficient_history": insufficient, "min_pct": min_pct, "window_days": window_days}


def trajectory(path: str | pathlib.Path, unit_id: str) -> list[dict]:
    """Ordered observation history for one unit:
    [{"run_id", "observed_at", "price", "status"}, ...], oldest first.

    This is the raw series sparklines and market-distress / negotiation-
    anchor features (other epics) are built from. Intentionally
    unfiltered: includes null prices and every status so a consumer can
    see gaps and "gone" runs, not just the priced points.
    """
    rows = [r for r in _rows(path) if r["unit_id"] == unit_id]
    rows.sort(key=lambda r: _parse_ts(r["observed_at"]))
    return [
        {"run_id": r["run_id"], "observed_at": r["observed_at"], "price": r["price"], "status": r["status"]}
        for r in rows
    ]


# --------------------------------------------------------------------------
# digest + heartbeat
# --------------------------------------------------------------------------


def digest_markdown(
    path: str | pathlib.Path,
    now: datetime.datetime | str | None = None,
    drop_pct: float = DEFAULT_DROP_PCT,
    drop_window_days: int = DEFAULT_DROP_WINDOW_DAYS,
) -> str:
    """Render the "What changed this week" section as a Markdown string.

    Always returns a renderable string - never raises - so a caller can
    drop the result straight into dashboard.html and the run report:
      - fewer than two runs: renders the loud insufficient-history message
        instead of an empty digest.
      - two+ runs, zero changes: renders the explicit "No changes since
        last run" line naming both run_ids.
      - two+ runs with changes: New / Removed / Price changed / Price
        drops subsections, every entry deep-linked to the unit's url, plus
        a price-unknown note and the drop detector's insufficient-history
        tally so an empty drops list is never mistaken for full coverage.
    """
    lines = ["## What changed this week", ""]
    try:
        diff = diff_last_two(path)
    except InsufficientHistoryError as exc:
        lines.append(f"**{exc}**")
        lines.append("")
        lines.append("Run hydrate again once at least two runs are recorded to see a delta.")
        return "\n".join(lines)

    run_from, run_to = diff["run_from"], diff["run_to"]
    drops = detect_drops(path, drop_pct, drop_window_days, now=now)

    lines.append(f"Comparing run `{run_from}` -> run `{run_to}`.")
    lines.append("")

    total_changes = len(diff["new"]) + len(diff["removed"]) + len(diff["price_changed"])
    if total_changes == 0:
        lines.append(f"No changes since last run (run `{run_from}` -> run `{run_to}`).")
        lines.append("")
    else:
        lines.append(f"### New ({len(diff['new'])})")
        for u in diff["new"]:
            price = f"${u['price']:,.0f}/mo" if u["price"] is not None else "price MISSING"
            lines.append(f"- [{u['unit_id']}]({u['url']}) - {price} ({u['status']})")
        if not diff["new"]:
            lines.append("- none")
        lines.append("")

        lines.append(f"### Removed ({len(diff['removed'])})")
        for u in diff["removed"]:
            price = f"${u['price']:,.0f}/mo" if u["price"] is not None else "price MISSING"
            lines.append(f"- [{u['unit_id']}]({u['url']}) - last seen {price} ({u['status']})")
        if not diff["removed"]:
            lines.append("- none")
        lines.append("")

        lines.append(f"### Price changed ({len(diff['price_changed'])})")
        for c in diff["price_changed"]:
            sign = "+" if c["delta"] > 0 else ""
            pct_txt = f"{sign}{c['pct']}%" if c["pct"] is not None else "pct n/a"
            lines.append(f"- [{c['unit_id']}]({c['url']}): ${c['from']:,.0f} -> ${c['to']:,.0f} ({pct_txt})")
        if not diff["price_changed"]:
            lines.append("- none")
        lines.append("")

    if diff["price_unknown"]:
        lines.append(f"### Price unknown ({len(diff['price_unknown'])})")
        for u in diff["price_unknown"]:
            lines.append(
                f"- [{u['unit_id']}]({u['url']}): from {u['from']!r} to {u['to']!r} - not counted as a change"
            )
        lines.append("")

    lines.append(f"### Price drops (>= {drop_pct}% within {drop_window_days} days) ({len(drops['drops'])})")
    for d in drops["drops"]:
        lines.append(
            f"- [{d['unit_id']}]({d['url']}): ${d['from']:,.0f} -> ${d['to']:,.0f} "
            f"(-{d['pct']}% over {d['observed_days']} days)"
        )
    if not drops["drops"]:
        lines.append("- none")
    if drops["insufficient_history"]:
        lines.append(
            f"- ({drops['insufficient_history']} unit(s) excluded from drop detection: "
            "fewer than two priced observations)"
        )
    lines.append("")

    return "\n".join(lines)


def heartbeat_overdue(
    runlog_path: str | pathlib.Path,
    max_age_days: float,
    now: datetime.datetime | str | None = None,
) -> bool:
    """True when the newest run-log row is older than max_age_days, or
    when no row has ever been written. The dashboard freshness banner keys
    on this: a missing or stale heartbeat means the scheduler died, not
    that the market went quiet.
    """
    rows = _rows(runlog_path)
    if not rows:
        return True
    latest = max(_parse_ts(r["at"], "at") for r in rows)
    now_ts = _coerce_now(now)
    return (now_ts - latest) > datetime.timedelta(days=max_age_days)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _read_stdin_json() -> Any:
    data = sys.stdin.read()
    if not data.strip():
        raise ValueError("expected JSON on stdin, got nothing")
    return json.loads(data)


def _cmd_append(args: argparse.Namespace) -> int:
    payload = _read_stdin_json()
    if args.target == "snapshots":
        if not isinstance(payload, list):
            raise ValueError("append --target snapshots expects a JSON array of rows on stdin")
        count = append_rows(args.path, args.run_id, payload)
        print(json.dumps({"appended": count, "target": "snapshots", "path": args.path}))
    else:
        if not isinstance(payload, dict):
            raise ValueError("append --target runlog expects a JSON object of stats on stdin")
        row = append_runlog(args.path, args.run_id, payload, at=args.at)
        print(json.dumps({"appended": 1, "target": "runlog", "row": row}))
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    try:
        result = diff_last_two(args.path)
    except InsufficientHistoryError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def _cmd_drops(args: argparse.Namespace) -> int:
    result = detect_drops(args.path, args.min_pct, args.window_days, now=args.now)
    print(json.dumps(result, indent=2))
    return 0


def _cmd_digest(args: argparse.Namespace) -> int:
    print(digest_markdown(args.path, now=args.now, drop_pct=args.drop_pct, drop_window_days=args.window_days))
    return 0


def _cmd_runlog_check(args: argparse.Namespace) -> int:
    overdue = heartbeat_overdue(args.path, args.max_age_days, now=args.now)
    print(json.dumps({"overdue": overdue, "max_age_days": args.max_age_days}))
    return 1 if overdue else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="snapshots.py",
        description="Append-only market-memory ledger and delta engine.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_append = sub.add_parser("append", help="append rows (stdin) to the ledger or the run-log")
    p_append.add_argument("--run-id", required=True)
    p_append.add_argument("--target", choices=("snapshots", "runlog"), default="snapshots")
    p_append.add_argument("--path", default=None, help="defaults to the standard path for --target")
    p_append.add_argument("--at", default=None,
                          help="runlog target only: the heartbeat timestamp (ISO 8601); defaults to now")
    p_append.set_defaults(func=_cmd_append)

    p_diff = sub.add_parser("diff", help="two-run NEW / REMOVED / PRICE_CHANGED diff")
    p_diff.add_argument("--path", default=DEFAULT_SNAPSHOTS_PATH)
    p_diff.set_defaults(func=_cmd_diff)

    p_drops = sub.add_parser("drops", help="windowed price-drop detector")
    p_drops.add_argument("--path", default=DEFAULT_SNAPSHOTS_PATH)
    p_drops.add_argument("--min-pct", type=float, default=DEFAULT_DROP_PCT)
    p_drops.add_argument("--window-days", type=int, default=DEFAULT_DROP_WINDOW_DAYS)
    p_drops.add_argument("--now", default=None)
    p_drops.set_defaults(func=_cmd_drops)

    p_digest = sub.add_parser("digest", help="render the weekly delta digest as Markdown")
    p_digest.add_argument("--path", default=DEFAULT_SNAPSHOTS_PATH)
    p_digest.add_argument("--drop-pct", type=float, default=DEFAULT_DROP_PCT)
    p_digest.add_argument("--window-days", type=int, default=DEFAULT_DROP_WINDOW_DAYS)
    p_digest.add_argument("--now", default=None)
    p_digest.set_defaults(func=_cmd_digest)

    p_runlog = sub.add_parser("runlog-check", help="check whether the heartbeat run-log is overdue")
    p_runlog.add_argument("--path", default=DEFAULT_RUNLOG_PATH)
    p_runlog.add_argument("--max-age-days", type=float, default=DEFAULT_HEARTBEAT_MAX_AGE_DAYS)
    p_runlog.add_argument("--now", default=None)
    p_runlog.set_defaults(func=_cmd_runlog_check)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "append" and args.path is None:
        args.path = DEFAULT_SNAPSHOTS_PATH if args.target == "snapshots" else DEFAULT_RUNLOG_PATH
    try:
        return args.func(args)
    except LedgerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
