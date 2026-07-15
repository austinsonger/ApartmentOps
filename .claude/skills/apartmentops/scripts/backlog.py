#!/usr/bin/env python3
"""Sync actions.yml and build the hydrate-report backlog resurfacing section.

Two plain files, deliberately split by ownership (see
../references/actions.md for the full contract):

- ``apartmentops/data/actions.yml`` - USER-OWNED. One entry per unit id:
  ``{status, note, updated_at}``. The only automated write this module
  makes to it is appending a ``status: NEW`` entry for a unit that has no
  entry yet; every existing entry is copied through untouched. That append
  is a TEXT APPEND to the end of the existing file (see
  ``append_new_entries`` below), never a parse-and-rewrite of the whole
  document - a parse-and-rewrite would silently strip the user's comments
  and re-sort their hand-chosen entry order, which defeats the entire
  point of a hand-editable file.
- ``apartmentops/data/backlog-state.json`` - SYSTEM-OWNED. One entry per
  unit id: ``{resurfaced, last_run_id}``, the resurface-decay counter that
  keeps the backlog from nagging about the same unit forever.

Library functions (importable, no I/O):
    unit_key(unit) -> str
    validate_actions(actions) -> None                    (raises ValueError)
    sync_new_units(actions, verified_units, now) -> dict
    describe_sync(before, after) -> str
    build_backlog(verified_units, actions, scores, state, run_id,
                   top_n=5, max_resurface=3) -> dict
    backlog_markdown(result) -> str

Plain-file I/O helpers (CLI-facing, but plain functions - see the section
below for the rest):
    load_actions(path) -> dict
    append_new_entries(path, before, after) -> bool       (text append only)

``build_backlog`` selection rule, in full:

1. Status must be ``NEW`` (missing from actions.yml also counts as NEW).
   Any other status silences resurfacing immediately, and deletes the
   unit's decay state so a later flip back to NEW starts the count at
   zero rather than resuming a stale counter.
2. The unit must be currently live per verified.json (``gone``,
   ``recheck``, and unknown liveness are all excluded - and leave any
   existing decay state untouched, neither advancing nor resetting it,
   since the unit was not actually evaluated this run).
3. A unit's very first time being tracked never appears in a Backlog
   section - it is "news, not backlog" the run it was first verified. It
   is registered in state (resurfaced=0) and becomes eligible starting
   the next run whose run_id differs from the one that registered it.
4. Eligible units are ranked by score (``scores[unit_id]["composite"]``)
   descending and the top ``top_n`` are actually included in the backlog
   this run - only those burn a resurface strike. Units that were
   eligible but ranked below the cap are held back with their decay state
   unchanged, so they compete again next run instead of losing a strike
   for something the user never saw.
5. A live, NEW, eligible unit with no score is never guessed a rank; it
   is listed under "unscored" instead (uncapped) and does burn a strike,
   since it was shown to the user.
6. Once a unit's resurface count reaches ``max_resurface`` (default 3) it
   permanently demotes to the "stale" list on its next eligible run and
   stays there - not shown as a ranked backlog row again - until its
   status changes away from NEW (which deletes its state) and later back
   to NEW (which starts the count over).

Draft outreach text (``draft_outreach_note`` on every backlog/unscored row)
is built only from fields already present on the verified-unit record and
is meant to be copied, edited, and sent by the user. No code path in this
module sends it anywhere.

CLI usage:
    python3 backlog.py --verified apartmentops/data/verified.json \\
        --actions apartmentops/data/actions.yml \\
        --scores scores.json \\
        --state apartmentops/data/backlog-state.json \\
        --run-id hyd-20260715-090000

    Prints a JSON object with sync_summary, backlog, unscored, stale, and a
    rendered markdown section, to stdout. Appends newly-synced NEW entries
    to the end of actions.yml as text (only if there are any; existing
    content, comments included, is never touched) and, when --state is
    given, writes backlog-state.json back.

Requires: pip install pyyaml (only for actions.yml I/O; the library
functions above take plain dicts and need no third-party package).
"""

from __future__ import annotations

import argparse
import copy
import datetime
import json
import pathlib
import re
import sys

STATUSES = (
    "NEW",
    "ToContact",
    "Contacted",
    "TourBooked",
    "Toured",
    "Applied",
    "Rejected",
    "Skipped",
    "Secured",
)


# --------------------------------------------------------------------------
# Unit identity and liveness
# --------------------------------------------------------------------------


def _slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "unit"


def unit_key(unit: dict) -> str:
    """Derive the id joining verified.json, actions.yml, and backlog-state.json.

    Prefers an explicit ``unit_id`` field on the verified-unit record.
    Falls back to a slug of building + unit number, since verified.json
    does not yet guarantee an explicit id field: building "Example Tower 2"
    unit "3410" becomes "example-tower-2-3410". Note this fallback is NOT
    the same key style as the example keys in verify_units.py's docstring
    (e.g. "tower2-3410") - verify_units.py never derives a key itself, its
    "key" field is caller-supplied input, so there is no shared derivation
    to match. Whoever wires verified.json writing should adopt an explicit
    unit_id field eventually so this fallback stops being load-bearing.
    """
    explicit = unit.get("unit_id")
    if explicit:
        return str(explicit)
    building = unit.get("building") or "unknown-building"
    number = unit.get("unit") or "unknown-unit"
    return f"{_slugify(str(building))}-{_slugify(str(number))}"


def _liveness(unit: dict) -> str:
    """Normalize a verified.json unit's liveness to live|gone|recheck|unknown.

    Reads a tri-state ``status`` field if present (forward-compatible with
    richer liveness tracking), else falls back to the boolean ``live``
    field the current contracts.md defines.
    """
    status = unit.get("status")
    if isinstance(status, str) and status.lower() in {"live", "gone", "recheck"}:
        return status.lower()
    live = unit.get("live")
    if live is True:
        return "live"
    if live is False:
        return "gone"
    return "unknown"


def _deep_link(unit: dict) -> str | None:
    return unit.get("unit_deep_link") or unit.get("verify_url") or unit.get("url")


def _draft_outreach_note(unit_id: str, unit: dict) -> str:
    """Build outreach text for the user to copy, edit, and send themselves.

    Never sent by any code path in this module - display text only, built
    only from fields already present on the unit record.
    """
    building = unit.get("building")
    unit_no = unit.get("unit")
    address = unit.get("address")
    price = unit.get("rent_verified")
    if price is None:
        price = unit.get("rent_gross")

    subject = f"unit {unit_no}" if unit_no else "the listing"
    if building:
        subject += f" at {building}"
    sentence = f"Hi, I'm interested in {subject}"
    if address:
        sentence += f" ({address})"
    sentence += "."
    if isinstance(price, (int, float)):
        sentence += (
            f" I saw it listed around ${price:,.0f}/mo and would like to "
            "schedule a viewing or get current availability."
        )
    else:
        sentence += (
            " Could you confirm current availability and pricing, and let "
            "me know about scheduling a viewing?"
        )
    return sentence


# --------------------------------------------------------------------------
# actions.yml (user-owned)
# --------------------------------------------------------------------------


def validate_actions(actions: dict) -> None:
    """Raise ValueError naming the unit id and allowed values on any bad status.

    Unknown status values are never silently coerced.
    """
    for unit_id, record in actions.items():
        status = record.get("status") if isinstance(record, dict) else None
        if status not in STATUSES:
            allowed = ", ".join(STATUSES)
            raise ValueError(
                f"actions.yml: unit '{unit_id}' has invalid status "
                f"{status!r}; allowed values: {allowed}"
            )


def sync_new_units(actions: dict, verified_units: dict, now: str) -> dict:
    """Append status: NEW entries for units missing from actions.yml.

    Returns a NEW dict; never mutates ``actions`` in place and never
    touches a unit id that already has an entry - existing status, note,
    and updated_at are copied through byte-for-byte.
    """
    updated = copy.deepcopy(actions)
    for unit_id in verified_units:
        if unit_id not in updated:
            updated[unit_id] = {"status": "NEW", "note": "", "updated_at": now}
    return updated


def describe_sync(before: dict, after: dict) -> str:
    """One-line run-report summary of what sync_new_units just did."""
    added = [unit_id for unit_id in after if unit_id not in before]
    if not before and added:
        return f"actions.yml created; all {len(added)} units initialized as NEW"
    if not added:
        return "actions.yml: no changes"
    return f"actions.yml: {len(added)} NEW entries appended ({len(after)} units tracked)"


# --------------------------------------------------------------------------
# Backlog resurfacing with decay
# --------------------------------------------------------------------------


def build_backlog(
    verified_units: dict,
    actions: dict,
    scores: dict,
    state: dict,
    run_id: str,
    top_n: int = 5,
    max_resurface: int = 3,
) -> dict:
    """Select still-live, high-scoring NEW units the user never acted on.

    Args:
        verified_units: {unit_id: unit_record} - unit_record fields read
            here: area, live/status, unit_deep_link/verify_url/url,
            building, unit, address, rent_verified/rent_gross.
        actions: {unit_id: {status, note, updated_at}} - as returned by
            sync_new_units (or loaded straight from actions.yml).
        scores: {unit_id: {composite, band}} - composite is a number used
            for ranking; a unit absent here, or with composite None, is
            treated as unscored, never guessed a rank.
        state: {unit_id: {resurfaced, last_run_id}} - the previous
            backlog-state.json contents (or {} on first-ever run).
        run_id: identifier for the run being evaluated right now.
        top_n: max scored units actually shown in the backlog this run.
        max_resurface: appearances allowed before permanent demotion to
            the stale list.

    Returns:
        {"backlog": [...], "unscored": [...], "stale": [...], "state": {...}}
        where "state" is the new backlog-state.json contents to persist.
    """
    new_state = copy.deepcopy(state) if state else {}
    scored_candidates: list[tuple[float, str, int]] = []
    unscored_rows: list[dict] = []
    stale_rows: list[dict] = []

    for unit_id, unit in verified_units.items():
        record = actions.get(unit_id)
        status = record.get("status") if isinstance(record, dict) else "NEW"
        if status is None:
            status = "NEW"

        if status != "NEW":
            # A user-set status silences resurfacing immediately. Drop any
            # decay state so a later flip back to NEW starts at zero.
            new_state.pop(unit_id, None)
            continue

        if _liveness(unit) != "live":
            # gone / recheck / unknown - not evaluated this run; leave
            # existing decay state exactly as it was.
            continue

        prior = new_state.get(unit_id)
        if prior is None:
            # First time tracked: news, not backlog, this run.
            new_state[unit_id] = {"resurfaced": 0, "last_run_id": run_id}
            continue

        if prior.get("last_run_id") == run_id:
            # Already processed earlier in this same call - idempotent.
            continue

        resurfaced_so_far = prior.get("resurfaced", 0)
        if resurfaced_so_far >= max_resurface:
            new_state[unit_id] = {"resurfaced": resurfaced_so_far, "last_run_id": run_id}
            score_entry = scores.get(unit_id) or {}
            stale_rows.append(
                {
                    "unit_id": unit_id,
                    "score": score_entry.get("composite"),
                    "deep_link": _deep_link(unit),
                }
            )
            continue

        resurfaced_next = resurfaced_so_far + 1
        score_entry = scores.get(unit_id)
        composite = score_entry.get("composite") if score_entry else None
        if composite is None:
            new_state[unit_id] = {"resurfaced": resurfaced_next, "last_run_id": run_id}
            unscored_rows.append(
                {
                    "unit_id": unit_id,
                    "area": unit.get("area"),
                    "score": None,
                    "band": None,
                    "deep_link": _deep_link(unit),
                    "draft_outreach_note": _draft_outreach_note(unit_id, unit),
                }
            )
        else:
            # Held pending the top_n cut - state commits only for units
            # that actually make it into the backlog below.
            scored_candidates.append((composite, unit_id, resurfaced_next))

    scored_candidates.sort(key=lambda t: (-t[0], t[1]))
    included = scored_candidates[:top_n]

    backlog_rows: list[dict] = []
    for composite, unit_id, resurfaced_next in included:
        unit = verified_units[unit_id]
        new_state[unit_id] = {"resurfaced": resurfaced_next, "last_run_id": run_id}
        score_entry = scores.get(unit_id) or {}
        backlog_rows.append(
            {
                "unit_id": unit_id,
                "area": unit.get("area"),
                "score": composite,
                "band": score_entry.get("band"),
                "deep_link": _deep_link(unit),
                "draft_outreach_note": _draft_outreach_note(unit_id, unit),
            }
        )
    # Units ranked below top_n are neither added to backlog_rows nor
    # committed to new_state: they are simply not shown this run and keep
    # whatever decay state they already had, ready to compete again next
    # run.

    return {
        "backlog": backlog_rows,
        "unscored": unscored_rows,
        "stale": stale_rows,
        "state": new_state,
    }


def backlog_markdown(result: dict) -> str:
    """Render a build_backlog() result as the hydrate-report Backlog section."""
    backlog = result.get("backlog", [])
    unscored = result.get("unscored", [])
    stale = result.get("stale", [])

    lines = ["## Backlog"]

    if not backlog and not unscored:
        lines.append("")
        lines.append("Backlog: none")
    else:
        for row in backlog:
            score = row.get("score")
            score_text = f"{score:.2f}" if isinstance(score, (int, float)) else "n/a"
            band = row.get("band") or "n/a"
            area = row.get("area") or "n/a"
            link = row.get("deep_link") or "n/a"
            lines.append("")
            lines.append(
                f"- **{row['unit_id']}** - score {score_text} ({band}) - "
                f"area: {area} - [Listing]({link})"
            )
            lines.append(f"  Draft outreach: \"{row['draft_outreach_note']}\"")

        if unscored:
            lines.append("")
            lines.append("### Unscored")
            for row in unscored:
                area = row.get("area") or "n/a"
                link = row.get("deep_link") or "n/a"
                lines.append(
                    f"- {row['unit_id']} - area: {area} - score: n/a - "
                    f"[Listing]({link})"
                )
                lines.append(f"  Draft outreach: \"{row['draft_outreach_note']}\"")

    if stale:
        lines.append("")
        lines.append("### Stale backlog")
        for row in stale:
            score = row.get("score")
            score_text = f"{score:.2f}" if isinstance(score, (int, float)) else "n/a"
            link = row.get("deep_link") or "n/a"
            lines.append(f"- {row['unit_id']} - score {score_text} - [Listing]({link})")

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Plain-file I/O (CLI only - library functions above take plain dicts)
# --------------------------------------------------------------------------


def load_actions(path) -> dict:
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "actions.yml requires PyYAML: pip install pyyaml"
        ) from exc
    data = yaml.safe_load(p.read_text())
    if not data:
        return {}
    if isinstance(data, dict) and "units" in data and isinstance(data["units"], dict):
        # Accept the earlier nested "units:" sketch for backward
        # compatibility; everything this codebase writes uses the flat form.
        return data["units"]
    if isinstance(data, dict):
        return data
    return {}


def _render_actions_entries_yaml(entries: dict) -> str:
    """Render a flat unit_id -> record mapping as YAML text.

    Used only to render a standalone block of NEWLY appended entries -
    never to re-render the whole actions.yml document. Re-rendering the
    whole document through yaml.safe_dump is exactly the bug this module
    used to have: it silently deletes every user comment and re-sorts
    hand-arranged entries. Rendering only the new block and appending it
    as text avoids touching a single existing byte.
    """
    if not entries:
        return ""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "actions.yml requires PyYAML: pip install pyyaml"
        ) from exc
    return yaml.safe_dump(entries, sort_keys=True, default_flow_style=False)


def append_new_entries(path, before: dict, after: dict) -> bool:
    """Append newly-synced NEW entries to actions.yml as text; never rewrite.

    ``before`` and ``after`` are actions dicts as returned by
    ``load_actions`` / ``sync_new_units`` - ``after`` must equal ``before``
    plus zero or more freshly-appended units (exactly what
    ``sync_new_units`` produces). Only the units present in ``after`` and
    absent from ``before`` are rendered and appended to the end of the
    file; every existing byte already on disk (comments, blank lines,
    hand-chosen key order, quoting) is left completely untouched, because
    the existing file content is read as raw text and never parsed back
    into a dict before being written out again.

    Returns True if the file was written, False if there was nothing new
    to append - in which case the file (if it even exists) is not opened
    for writing at all, matching the "actions.yml: no changes" no-op path.
    """
    new_entries = {
        unit_id: record for unit_id, record in after.items() if unit_id not in before
    }
    if not new_entries:
        return False

    appended_yaml = _render_actions_entries_yaml(new_entries)

    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing_text = p.read_text() if p.exists() else ""

    if not existing_text:
        p.write_text(appended_yaml)
        return True

    separator = "\n" if existing_text.endswith("\n") else "\n\n"
    p.write_text(existing_text + separator + appended_yaml)
    return True


def load_verified_units(path) -> dict:
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    if isinstance(data, dict):
        return data
    return {unit_key(unit): unit for unit in data}


def load_json_dict(path) -> dict:
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    return data if isinstance(data, dict) else {}


def save_json_dict(path, data: dict) -> None:
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync actions.yml and build the hydrate backlog section."
    )
    parser.add_argument("--verified", required=True, help="path to verified.json")
    parser.add_argument("--actions", required=True, help="path to actions.yml")
    parser.add_argument(
        "--scores", default=None, help="path to a JSON file of {unit_id: {composite, band}}"
    )
    parser.add_argument("--state", default=None, help="path to backlog-state.json")
    parser.add_argument("--run-id", required=True, help="identifier for this hydrate run")
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--max-resurface", type=int, default=3)
    parser.add_argument(
        "--now",
        default=None,
        help="ISO date for newly appended NEW entries (default: today, local date)",
    )
    args = parser.parse_args()

    now = args.now or datetime.date.today().isoformat()

    verified_units = load_verified_units(args.verified)
    actions_before = load_actions(args.actions)
    validate_actions(actions_before)
    actions_after = sync_new_units(actions_before, verified_units, now)
    append_new_entries(args.actions, actions_before, actions_after)

    scores = load_json_dict(args.scores) if args.scores else {}
    state = load_json_dict(args.state) if args.state else {}

    result = build_backlog(
        verified_units,
        actions_after,
        scores,
        state,
        args.run_id,
        top_n=args.top_n,
        max_resurface=args.max_resurface,
    )

    if args.state:
        save_json_dict(args.state, result["state"])

    output = {
        "sync_summary": describe_sync(actions_before, actions_after),
        "backlog": result["backlog"],
        "unscored": result["unscored"],
        "stale": result["stale"],
        "markdown": backlog_markdown(result),
    }
    json.dump(output, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
