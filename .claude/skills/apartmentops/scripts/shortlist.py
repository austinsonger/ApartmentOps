#!/usr/bin/env python3
"""Shortlist sync planning: rows, the link gate, and the read-back diff.

The apartmentops-sync skill keeps a user's shortlist document (a Google
Sheet, or a table in a Google Doc) in step with verified.json. This module
is the deterministic half: it builds the rows, plans what to append and
which Link cells to refresh, judges each link from evidence the skill
collected in a browser, and diffs what the document holds after the write
against what was planned. It never touches the network or the document -
the skill does the browsing and the connector calls.

Machine fields are the pipeline's (`MACHINE_FIELDS`); every other configured
column belongs to the user and is written as an empty string on append and
never compared or overwritten afterwards.

Stdlib only.

CLI:
    python3 shortlist.py rows verified.json buildings.json actions.yml config.yml
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
import re
import sys
from typing import Any

MACHINE_FIELDS = ("num", "address", "name", "walkScore", "price", "sqft", "availability", "link")
EXCLUDED_STATUSES = ("Rejected", "Skipped")
MAX_RECOVERY_ATTEMPTS = 3

# A final URL that is a search or results page is not a listing: the
# listing redirected away, or the link never pointed at a unit.
SEARCH_URL = re.compile(
    r"/(search|results)(/|\?|$)|[?&](q|query|search)=|/rentals/?(\?|$)|"
    r"/apartments-for-rent/?(\?|$)|/for-rent-by-owner/?(\?|$)",
    re.IGNORECASE,
)
_DIRECTIONALS = {"n", "s", "e", "w", "ne", "nw", "se", "sw", "north", "south", "east", "west"}
_UNIT_WORDS = re.compile(r"\b(apt|unit|ste|suite)\b.*$|#.*$", re.IGNORECASE)


# ------------------------------------------------------------------ columns

def validate_columns(columns) -> None:
    """Raise ValueError naming the problem when `address` or `link` is not
    configured, or a field or a label repeats."""
    columns = list(columns or [])
    fields = [c.get("field") for c in columns]
    labels = [c.get("label") for c in columns]
    for required in ("address", "link"):
        if required not in fields:
            raise ValueError(f"shortlist_sync.columns has no '{required}' column; it is required")
    for name, values in (("field", fields), ("label", labels)):
        seen = set()
        for v in values:
            if v in seen:
                raise ValueError(f"shortlist_sync.columns repeats {name} {v!r}")
            seen.add(v)


def norm_address(s: str | None) -> str:
    """Street portion before the first comma, lowercase, apt/unit/ste/suite
    to '#', keeping [a-z0-9#] only: "123 N Main St Apt 4B, Chicago, IL" ->
    "123nmainst#4b". The row key that matches a document row to a unit."""
    street = (s or "").split(",", 1)[0].lower()
    street = re.sub(r"\b(apt|unit|ste|suite)\b\.?", "#", street)
    return re.sub(r"[^a-z0-9#]", "", street)


# --------------------------------------------------------------------- rows

def _val(unit: dict, key: str):
    v = unit.get(key)
    if isinstance(v, dict) and "value" in v:
        return v.get("value")
    return v


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")


def _units(verified_units) -> dict:
    if isinstance(verified_units, dict):
        return verified_units
    import backlog  # local module; same unit_id rule everywhere

    return {backlog.unit_key(u): u for u in verified_units or []}


def _address_with_unit(unit: dict) -> str:
    address = str(_val(unit, "address") or "")
    number = _val(unit, "unit")
    if not number or norm_address(address).find("#") >= 0:
        return address
    street, _, rest = address.partition(",")
    out = f"{street.strip()} Unit {number}"
    return f"{out},{rest}" if rest else out


def _status(actions: dict, unit_id: str) -> str | None:
    record = (actions or {}).get(unit_id)
    return record.get("status") if isinstance(record, dict) else None


def build_rows(verified_units, buildings: dict, scores: dict, actions: dict, columns) -> list[dict]:
    """One row per live unit whose actions.yml status is not Rejected or
    Skipped, ordered by composite score (unscored last), then unit_id.

    Machine fields: address (with the unit), name (building; "Private" for
    an owner ad with no building), walkScore (buildings[slug].walk.score),
    price (rent_verified), sqft, availability (available), link
    (unit_deep_link, else verify_url, else url). num is assigned at plan
    time. Every other configured column is "". Each row also carries
    `key` (norm_address of the address) and `unit_id`.
    """
    buildings = buildings or {}
    scores = scores or {}
    user_fields = [c["field"] for c in columns or [] if c.get("field") not in MACHINE_FIELDS]
    rows = []
    for unit_id, unit in _units(verified_units).items():
        if unit.get("live") is not True:
            continue
        if _status(actions, unit_id) in EXCLUDED_STATUSES:
            continue
        building = _val(unit, "building") or _val(unit, "property_name")
        owner = _val(unit, "advertiser_type") == "owner"
        slug = _slugify(str(building)) if building else ""
        walk = (buildings.get(slug) or {}).get("walk") or {}
        address = _address_with_unit(unit)
        row = {
            "num": "",
            "address": address,
            "name": building or ("Private" if owner else ""),
            "walkScore": walk.get("score"),
            "price": _val(unit, "rent_verified"),
            "sqft": _val(unit, "sqft"),
            "availability": _val(unit, "available"),
            "link": unit.get("unit_deep_link") or unit.get("verify_url") or unit.get("url"),
        }
        for field in user_fields:
            row[field] = ""
        row["key"] = norm_address(address)
        row["unit_id"] = unit_id
        rows.append(row)

    def order(row):
        composite = (scores.get(row["unit_id"]) or {}).get("composite")
        return (composite is None, -(composite or 0), row["unit_id"])

    return sorted(rows, key=order)


# --------------------------------------------------------------------- plan

def _as_datetime(value) -> _dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value
    return _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def plan_sync(doc_rows, new_rows, last_sync, now, min_days) -> dict:
    """What a sync would do.

    doc_rows: [{key, row_index, link}] discovered across every table in the
    document. new_rows: build_rows output. Rows whose key is not in the
    document go to to_append (numbered after the document's rows); rows
    whose key is present with a different link go to to_refresh. run is
    True when there is anything to append, on the first sync, or once
    min_days have passed since last_sync.
    """
    doc_rows = list(doc_rows or [])
    by_key = {r["key"]: r for r in doc_rows}
    to_append, to_refresh = [], []
    next_num = len(doc_rows) + 1
    for row in new_rows or []:
        existing = by_key.get(row["key"])
        if existing is None:
            to_append.append(dict(row, num=next_num))
            next_num += 1
        elif row.get("link") and (existing.get("link") or "") != row["link"]:
            to_refresh.append({"row_index": existing["row_index"], "key": row["key"], "link": row["link"]})

    last = _as_datetime(last_sync)
    current = _as_datetime(now)
    if to_append:
        run, reason = True, f"{len(to_append)} new rows to append"
    elif last is None:
        run, reason = True, "first sync"
    elif current - last >= _dt.timedelta(days=min_days):
        run, reason = True, f"{(current - last).days} days since last sync (cadence {min_days})"
    else:
        run, reason = False, f"nothing new and last sync was {(current - last).days} days ago (cadence {min_days})"
    return {"run": run, "reason": reason, "to_append": to_append, "to_refresh": to_refresh}


# --------------------------------------------------------------- link gate

def _street_parts(address: str) -> tuple[str | None, list[str]]:
    street = _UNIT_WORDS.sub(" ", (address or "").split(",", 1)[0].lower())
    words = re.findall(r"[a-z0-9]+", street)
    if not words or not words[0].isdigit():
        return None, []
    rest = [w for w in words[1:] if w not in _DIRECTIONALS]
    # drop a trailing suffix (st, ave, blvd, ...) and anything after it
    name = rest[:-1] if len(rest) > 1 else rest
    return words[0], name


def _address_check(address: str, haystack: str) -> str:
    """'match', 'abbreviated' (number matches, street name only as an
    abbreviation), or 'mismatch'."""
    number, name = _street_parts(address)
    if number is None or not re.search(rf"\b{number}\b", haystack):
        return "mismatch"
    if not name:
        return "match"
    if re.search(r"\b" + r"\s+".join(name) + r"\b", haystack):
        return "match"
    words = re.findall(r"[a-z0-9]+", haystack)
    initials = "".join(w[0] for w in name)
    for w in words:
        if len(name) > 1 and w == initials:
            return "abbreviated"
        if len(w) >= 3 and name[0].startswith(w) and w != name[0]:
            return "abbreviated"
    return "mismatch"


def link_gate(row: dict, evidence: dict) -> dict:
    """Judge one row's link from browser evidence.

    evidence: {final_url, status, page_text_excerpt, availability_signal,
    whole_unit, unit_level}. Checks, in order: live (HTTP 200 and the final
    URL is not a search or results page), address match (street number and
    name in the excerpt or the final URL), active (availability_signal),
    whole unit, direct-to-unit (unit_level). FLAG only when every check
    passes except that the street name appears abbreviated next to a
    matching number.
    """
    evidence = evidence or {}
    final_url = evidence.get("final_url") or ""
    if evidence.get("status") != 200:
        return {"verdict": "FAIL", "label": "dead", "reason": f"HTTP {evidence.get('status')}"}
    if SEARCH_URL.search(final_url):
        return {"verdict": "FAIL", "label": "dead", "reason": f"redirected to a search or results page: {final_url}"}
    haystack = ((evidence.get("page_text_excerpt") or "") + " "
                + re.sub(r"[-_/+.]", " ", final_url)).lower()
    address = _address_check(row.get("address") or "", haystack)
    if address == "mismatch":
        return {"verdict": "FAIL", "label": "non_direct", "reason": "street number and name not found on the page"}
    if not evidence.get("availability_signal"):
        return {"verdict": "FAIL", "label": "dead", "reason": "no availability signal on the page"}
    if not evidence.get("whole_unit"):
        return {"verdict": "FAIL", "label": "non_direct", "reason": "not a whole unit (room, shared, or sublet)"}
    if not evidence.get("unit_level"):
        return {"verdict": "FAIL", "label": "non_direct", "reason": "building or floor-plan page, not the unit's own page"}
    if address == "abbreviated":
        return {"verdict": "FLAG", "label": None, "reason": "number matches; street name abbreviated on the page"}
    return {"verdict": "PASS", "label": None, "reason": "live, matching, active, whole unit, unit-level"}


def gate_summary(verdicts) -> dict:
    """Counts over gate results: [{verdict: PASS|FLAG|FAIL|None, label,
    held: bool}]. A held row (no link, or recovery exhausted) is not
    written and not counted as a failure. ready is True only when no
    unheld row failed and every row with no verdict is held."""
    out = {"pass": 0, "flag": 0, "fail_dead": 0, "fail_non_direct": 0, "held": 0}
    unresolved = 0
    for v in verdicts or []:
        if v.get("held"):
            out["held"] += 1
        elif v.get("verdict") == "PASS":
            out["pass"] += 1
        elif v.get("verdict") == "FLAG":
            out["flag"] += 1
        elif v.get("verdict") == "FAIL":
            out["fail_dead" if v.get("label") == "dead" else "fail_non_direct"] += 1
        else:
            unresolved += 1
    out["ready"] = out["fail_dead"] == 0 and out["fail_non_direct"] == 0 and unresolved == 0
    return out


def recovery_budget(state: dict, key: str) -> bool:
    """Count one recovery attempt for a row key in shortlist-state.json's
    `recovery` map; False once MAX_RECOVERY_ATTEMPTS have been spent."""
    counts = state.setdefault("recovery", {})
    counts[key] = counts.get(key, 0) + 1
    return counts[key] <= MAX_RECOVERY_ATTEMPTS


def load_state(path) -> dict:
    p = pathlib.Path(path)
    if not p.exists():
        return {"recovery": {}}
    data = json.loads(p.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {"recovery": {}}


def save_state(path, state: dict) -> None:
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- read-back

def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def readback_diff(expected_rows, observed_rows) -> list[dict]:
    """Cell-level differences on machine fields only, matched by key. A
    planned row missing from the document is one {key, field: None,
    expected: "row", observed: None} entry. User columns are never
    compared. An empty list proves every planned cell landed."""
    observed = {r.get("key"): r for r in observed_rows or []}
    diffs = []
    for row in expected_rows or []:
        got = observed.get(row["key"])
        if got is None:
            diffs.append({"key": row["key"], "field": None, "expected": "row", "observed": None})
            continue
        for field in MACHINE_FIELDS:
            if field in row and _cell(row[field]) != _cell(got.get(field)):
                diffs.append({"key": row["key"], "field": field,
                              "expected": _cell(row[field]), "observed": _cell(got.get(field))})
    return diffs


# ---------------------------------------------------------------------- CLI

def _load(path: str):
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    if p.suffix in (".yml", ".yaml"):
        import yaml  # pip install pyyaml

        return yaml.safe_load(text) or {}
    return json.loads(text)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 5 or args[0] != "rows":
        print(__doc__)
        return 2
    verified, buildings, actions, config = (_load(a) for a in args[1:])
    columns = ((config or {}).get("shortlist_sync") or {}).get("columns") or [
        {"field": f, "label": f} for f in MACHINE_FIELDS]
    validate_columns(columns)
    rows = build_rows(verified, buildings, {}, actions, columns)
    print(json.dumps(rows, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
