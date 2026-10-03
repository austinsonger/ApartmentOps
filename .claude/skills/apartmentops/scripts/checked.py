#!/usr/bin/env python3
"""Hunt memory: rejection ledger, deferred pools, scan coverage, and the
drain-as-you-go candidate file.

Without this file, separate sessions re-open listings that were already
looked at and dismissed. It also records what each scan actually covered,
so an interrupted scan is never reported as a complete one.

File: apartmentops/data/checked.json (pipeline state, never shown to a
landlord, never published). Shape::

    {
      "version": 1,
      "rejected": {"<token>": {"source", "reason", "checked_at",
                               "criteria", "criteria_dependent"}},
      "deferred": {"<group>": {"<token>": {"reason", "deferred_at"}}},
      "out_of_window": {"<token>": {"published_at", "checked_at"}},
      "scan_state": {"<run_id>": {"started_at", "queries": {"<query>":
                     {"pages_expected", "pages_scanned": [..]}},
                     "blockers": [..], "finished_at"}},
      "price_refresh": {"<run_id>": {"verified", "missing", "unverified",
                        "changes"}}
    }

- `rejected` - opened and dismissed, with the reason and the date.
- `deferred` - filtered out BEFORE opening (out of area, over budget).
  Never opened, so they are the first candidates when the user widens the
  search.
- `out_of_window` - already checked and found older than the target
  publication window; without it every incremental round re-fetches the
  whole old tail.
- `scan_state` / `price_refresh` - what a round covered and by what method.

The drained-candidates file (apartmentops/data/drain/<run_id>.jsonl) is
the other half: browser page state is volatile (a CAPTCHA redirect wipes
it without warning), so candidate tokens and item details are appended to
disk in small batches as they arrive, never held in page variables or the
scraped origin's localStorage until the end.

Stdlib only, no network.

CLI:
    python3 checked.py fingerprint config.yml|config.json
    python3 checked.py coverage checked.json RUN_ID
    python3 checked.py filter checked.json FINGERPRINT < tokens.json
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import pathlib
import sys
import tempfile
from typing import Any, Iterable

VERSION = 1
DEFAULT_REJECTION_MAX_AGE_DAYS = 30
CRITERIA_KEYS = ("budget", "unit", "gates", "geography", "locale")


def empty_state() -> dict:
    return {
        "version": VERSION,
        "rejected": {},
        "deferred": {},
        "out_of_window": {},
        "scan_state": {},
        "price_refresh": {},
    }


def load(path) -> dict:
    """Load checked.json; a missing file is a fresh, empty memory."""
    p = pathlib.Path(path)
    if not p.exists():
        return empty_state()
    data = json.loads(p.read_text(encoding="utf-8"))
    state = empty_state()
    state.update(data)
    return state


def save(path, state: dict) -> None:
    """Atomic write (temp file + rename) so a crash never truncates memory."""
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".checked-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, p)


def _parse(ts: str) -> _dt.datetime:
    value = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {ts!r}")
    return value


def criteria_fingerprint(config: dict) -> str:
    """Short stable hash of the search criteria a rejection depended on.

    Only the criteria blocks count (budget, unit, gates, geography,
    locale); renaming the anchor label or editing notes does not expire
    every rejection.
    """
    subset = {k: config.get(k) for k in CRITERIA_KEYS if k in config}
    blob = json.dumps(subset, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def record_rejection(
    state: dict,
    token: str,
    reason: str,
    checked_at: str,
    criteria: str,
    *,
    source: str | None = None,
    criteria_dependent: bool = True,
) -> None:
    """Remember that a listing was opened and dismissed.

    criteria_dependent=False is for reasons that hold under any search
    (a sublet, a scam flag, rent not stated); those survive a criteria
    change and only expire by age.
    """
    if not token or not reason:
        raise ValueError("token and reason are required")
    _parse(checked_at)
    state["rejected"][str(token)] = {
        "source": source,
        "reason": reason,
        "checked_at": checked_at,
        "criteria": criteria,
        "criteria_dependent": bool(criteria_dependent),
    }


def rejection_active(
    state: dict,
    token: str,
    criteria: str,
    now: str,
    max_age_days: int = DEFAULT_REJECTION_MAX_AGE_DAYS,
) -> bool:
    """True when a prior rejection still applies.

    A rejection expires when it is older than max_age_days (evidence goes
    stale), or when it was criteria-dependent and the criteria changed.
    """
    entry = state["rejected"].get(str(token))
    if not entry:
        return False
    age = _parse(now) - _parse(entry["checked_at"])
    if age.days > max_age_days:
        return False
    if entry.get("criteria_dependent", True) and entry.get("criteria") != criteria:
        return False
    return True


def record_deferred(state: dict, group: str, token: str, reason: str, deferred_at: str) -> None:
    """A candidate filtered out before opening (e.g. group='out_of_area')."""
    _parse(deferred_at)
    state["deferred"].setdefault(group, {})[str(token)] = {
        "reason": reason,
        "deferred_at": deferred_at,
    }


def record_out_of_window(state: dict, token: str, published_at: str, checked_at: str) -> None:
    _parse(checked_at)
    state["out_of_window"][str(token)] = {
        "published_at": published_at,
        "checked_at": checked_at,
    }


def filter_new(
    tokens: Iterable[str],
    known_tokens: Iterable[str],
    state: dict,
    criteria: str,
    now: str,
    max_age_days: int = DEFAULT_REJECTION_MAX_AGE_DAYS,
) -> dict:
    """Split feed tokens into the ones worth opening and the ones skipped.

    known_tokens is every token already in verified.json - live AND gone,
    so a delisted unit that reappears goes through dedupe, not "new".
    Returns {"open": [...], "skipped": {token: reason}} in input order.
    """
    known = {str(t) for t in known_tokens}
    to_open: list[str] = []
    skipped: dict[str, str] = {}
    seen: set[str] = set()
    for raw in tokens:
        token = str(raw)
        if token in seen:
            continue
        seen.add(token)
        if token in known:
            skipped[token] = "already tracked"
        elif rejection_active(state, token, criteria, now, max_age_days):
            skipped[token] = "rejected: " + state["rejected"][token]["reason"]
        elif token in state["out_of_window"]:
            skipped[token] = "out of publication window"
        else:
            to_open.append(token)
    return {"open": to_open, "skipped": skipped}


# ---------------------------------------------------------------- scan state

def start_scan(state: dict, run_id: str, started_at: str) -> None:
    _parse(started_at)
    state["scan_state"].setdefault(
        run_id, {"started_at": started_at, "queries": {}, "blockers": [], "finished_at": None}
    )


def set_pages_expected(state: dict, run_id: str, query: str, pages_expected: int) -> None:
    """Record the page count the source itself reported (never a page count
    computed from an assumed page size)."""
    q = state["scan_state"][run_id]["queries"].setdefault(
        query, {"pages_expected": None, "pages_scanned": []}
    )
    q["pages_expected"] = int(pages_expected)


def mark_page(state: dict, run_id: str, query: str, page: int) -> None:
    q = state["scan_state"][run_id]["queries"].setdefault(
        query, {"pages_expected": None, "pages_scanned": []}
    )
    if page not in q["pages_scanned"]:
        q["pages_scanned"].append(int(page))
        q["pages_scanned"].sort()


def add_blocker(state: dict, run_id: str, kind: str, detail: str, at: str) -> None:
    """kind: captcha | bot_wall | throttled | fetch_error | login_wall."""
    _parse(at)
    state["scan_state"][run_id]["blockers"].append({"kind": kind, "detail": detail, "at": at})


def finish_scan(state: dict, run_id: str, finished_at: str) -> None:
    _parse(finished_at)
    state["scan_state"][run_id]["finished_at"] = finished_at


def coverage(state: dict, run_id: str) -> dict:
    """What a scan really covered. complete is True only when every query
    reported its page count, every page was read, no blocker was hit, and
    the run was finished - anything less is partial coverage."""
    run = state["scan_state"].get(run_id)
    if run is None:
        raise KeyError(f"no scan_state for run {run_id!r}")
    rows = []
    complete = run.get("finished_at") is not None and not run["blockers"]
    for query, q in sorted(run["queries"].items()):
        expected = q.get("pages_expected")
        scanned = q.get("pages_scanned", [])
        missing = (
            [p for p in range(1, expected + 1) if p not in scanned] if expected else None
        )
        ok = expected is not None and not missing
        complete = complete and ok
        rows.append({
            "query": query,
            "pages_expected": expected,
            "pages_scanned": len(scanned),
            "missing_pages": missing,
            "complete": ok,
        })
    if not rows:
        complete = False
    return {"run_id": run_id, "complete": complete, "queries": rows, "blockers": run["blockers"]}


def record_price_refresh(
    state: dict,
    run_id: str,
    verified: list,
    missing: list,
    unverified: list,
    changes: list,
) -> None:
    state["price_refresh"][run_id] = {
        "verified": list(verified),
        "missing": list(missing),
        "unverified": list(unverified),
        "changes": list(changes),
    }


# ------------------------------------------------------------- drain file

def drain_append(path, records: list[dict]) -> int:
    """Append a small batch of records (each needs a 'token') to a JSONL
    drain file immediately. Validates the whole batch before writing."""
    for rec in records:
        if not isinstance(rec, dict) or not rec.get("token"):
            raise ValueError(f"drain record needs a token: {rec!r}")
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    return len(records)


def load_drained(path) -> dict[str, dict]:
    """Read a drain file back, keyed by token; a later line for the same
    token merges over an earlier one (feed row first, item details later).
    A truncated final line (crash mid-write) is skipped, not fatal."""
    p = pathlib.Path(path)
    out: dict[str, dict] = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        token = str(rec.get("token"))
        out.setdefault(token, {}).update(rec)
    return out


def _load_config(path: str) -> dict:
    if path.endswith((".yml", ".yaml")):
        try:
            import yaml  # type: ignore
        except ImportError:
            sys.exit("reading a .yml config needs PyYAML: pip install pyyaml")
        return yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")) or {}
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__)
        return 2
    cmd = args[0]
    if cmd == "fingerprint" and len(args) == 2:
        print(criteria_fingerprint(_load_config(args[1])))
        return 0
    if cmd == "coverage" and len(args) == 3:
        print(json.dumps(coverage(load(args[1]), args[2]), indent=2))
        return 0
    if cmd == "filter" and len(args) == 3:
        tokens = json.load(sys.stdin)
        now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        print(json.dumps(filter_new(tokens, [], load(args[1]), args[2], now), indent=2))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
