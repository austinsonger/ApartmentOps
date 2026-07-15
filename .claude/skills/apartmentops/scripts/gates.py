#!/usr/bin/env python3
"""Provenance helpers and tri-state hard-gate evaluation.

See ../references/provenance.md for the full field-shape and gate contract
this module implements. Short version:

Field shape (any value stored under a "verified" record, e.g. in
apartmentops/data/verified.json)::

    {"value": 2450, "status": "FACT", "source": "https://...",
     "evidence": "apartmentops/shots/unit.png", "confidence": null}

status is one of FACT, INFERRED, MISSING, CONFLICT (see the STATUSES tuple
below). A field can also be a bare legacy scalar (or null) from before this
schema existed - normalize_field() treats a non-null scalar as FACT with
source null, and null as MISSING.

Hard gates (evaluate_gates) turn a unit's fields into PASS / FAIL / UNKNOWN
per gate, honoring hard vs bonus mode. This module does not know how to
translate apartmentops/config.yml's ad hoc `gates:` block into gate
definitions on its own - config.yml mixes hard/bonus modes, comparison
styles, and preference lists in ways specific to that file. Instead,
evaluate_gates takes an explicit, generic gate-spec dict so it stays a pure,
testable function; see the module docstring's "config_gates shape" section
and references/provenance.md for the concrete config.yml -> config_gates
translation the scan/verify integration should perform.

config_gates shape::

    {
      "in_unit_laundry": {"field": "in_unit_laundry", "op": "eq",
                           "value": True, "mode": "hard"},
      "floor_min":       {"field": "floor", "op": "gte",
                           "value": 10, "mode": "bonus"},
      "rent_max":        {"field": "rent_verified", "op": "lte",
                           "value": 4900, "mode": "hard"},
    }

"field" names a key in the unit dict (defaults to the gate name itself if
omitted). "op" is one of eq, ne, gte, lte, gt, lt, in, not_in. "mode" is
"hard" (a violated FACT excludes the unit; an unresolved field blocks
TourNow) or "bonus" (never produces FAIL - an unmet or unresolved bonus
gate reads UNKNOWN, and bonus criteria are expected to feed the scoring
rubric elsewhere, not the tri-state gates block).

IMPORTANT: verify_checklist() and tour_now_blocked() treat every key in the
gate_results dict they are given as something that must PASS before
TourNow. Callers should therefore build the "gates" block that feeds those
two functions (and gets written into verified.json) from hard-mode gates
only - evaluate a bonus-only config_gates subset separately for scoring.

grade_fields() is a secondary, generically-applicable helper implementing
the structurally-checkable subset of references/constraints.json's
autofail rules (value-without-provenance and placeholder-left-in-output
apply to any dict; grade-without-source, price-not-number, and
unverified-marked-live apply using the field names from
references/contracts.md's areas.json/verified.json shapes). It is not a
full replacement for the verifier pass described in provenance.md, which
also needs domain knowledge (e.g. which unit a field belongs to) that lives
in the scan/verify integration.

CLI:
    python3 gates.py unit.json config_gates.json [constraints.json]
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Provenance field shape
# ---------------------------------------------------------------------------

FACT = "FACT"
INFERRED = "INFERRED"
MISSING = "MISSING"
CONFLICT = "CONFLICT"
STATUSES = (FACT, INFERRED, MISSING, CONFLICT)

# INFERRED fields below this confidence are discarded (downgraded to
# MISSING) rather than kept as a hedge. Exactly 0.3 stays INFERRED.
INFERRED_CONFIDENCE_FLOOR = 0.3

GATE_PASS = "PASS"
GATE_FAIL = "FAIL"
GATE_UNKNOWN = "UNKNOWN"


def _is_legacy_scalar(field: Any) -> bool:
    """A field is "legacy" if it is not a provenance dict, i.e. it predates
    this schema: a bare value (including None) rather than
    {"value":..., "status":...}."""
    return not (isinstance(field, dict) and "status" in field)


def field_value(field: Any) -> Any:
    """Return the field's value regardless of shape (legacy scalar or
    provenance dict). Does not apply the INFERRED confidence-floor rule -
    use normalize_field() first if that matters for your use case."""
    if _is_legacy_scalar(field):
        return field
    return field.get("value")


def field_status(field: Any) -> str:
    """Return the field's status regardless of shape. A non-null legacy
    scalar is FACT (source null); a null legacy scalar is MISSING. Raises
    ValueError if a provenance dict names an unrecognized status."""
    if _is_legacy_scalar(field):
        return MISSING if field is None else FACT
    status = field.get("status")
    if status not in STATUSES:
        raise ValueError(f"unknown field status: {status!r}")
    return status


def normalize_field(field: Any) -> dict[str, Any]:
    """Return the canonical provenance dict for `field`, applying the
    INFERRED-below-0.3-downgrades-to-MISSING rule and forcing value to null
    for MISSING/CONFLICT (a CONFLICT field's real values live in its
    "conflicts" citation list, never as a silent top-level winner).

    Legacy scalars are migrated in place: non-null -> FACT/source-null,
    null -> MISSING. This is the same rule the verified.json migration
    should apply per field.
    """
    if _is_legacy_scalar(field):
        if field is None:
            return {"value": None, "status": MISSING, "source": None, "evidence": None}
        return {"value": field, "status": FACT, "source": None, "evidence": None}

    status = field.get("status")
    if status not in STATUSES:
        raise ValueError(f"unknown field status: {status!r}")

    out = dict(field)
    if status == INFERRED:
        confidence = out.get("confidence")
        if confidence is None or confidence < INFERRED_CONFIDENCE_FLOOR:
            return {
                "value": None,
                "status": MISSING,
                "source": out.get("source"),
                "evidence": out.get("evidence"),
            }
    if status == MISSING:
        out["value"] = None
    if status == CONFLICT:
        out["value"] = None  # no silent winner; see out.get("conflicts")
    return out


# ---------------------------------------------------------------------------
# Tri-state hard gates
# ---------------------------------------------------------------------------

_OPS = {
    "eq": lambda value, target: value == target,
    "ne": lambda value, target: value != target,
    "gte": lambda value, target: value >= target,
    "lte": lambda value, target: value <= target,
    "gt": lambda value, target: value > target,
    "lt": lambda value, target: value < target,
    "in": lambda value, target: value in target,
    "not_in": lambda value, target: value not in target,
}
# Symbolic aliases so a caller writing ">=" gets the comparison it meant
# instead of a silent UNKNOWN.
_OPS.update({
    "==": _OPS["eq"], "!=": _OPS["ne"],
    ">=": _OPS["gte"], "<=": _OPS["lte"],
    ">": _OPS["gt"], "<": _OPS["lt"],
})


def evaluate_gates(config_gates: dict[str, dict[str, Any]], unit: dict[str, Any]) -> dict[str, str]:
    """Evaluate every gate in config_gates against `unit`'s fields.

    Returns {gate_name: "PASS"|"FAIL"|"UNKNOWN"}.

    - PASS: the field is a FACT (post-normalization) and it satisfies "op".
    - FAIL: the field is a FACT and it does NOT satisfy "op" - hard mode
      only. A bonus-mode gate never returns FAIL by design; an unmet bonus
      criterion reads UNKNOWN instead (it is scored elsewhere).
    - UNKNOWN: the field is INFERRED, MISSING, or CONFLICT (a gate needs a
      FACT; inference at any confidence still maps to UNKNOWN here), or the
      comparison itself could not be made (missing/incomparable op or
      value) - never guessed either way.
    """
    results: dict[str, str] = {}
    for gate_name, gate_def in config_gates.items():
        mode = gate_def.get("mode", "hard")
        field_name = gate_def.get("field", gate_name)
        op = gate_def.get("op")
        target = gate_def.get("value")

        raw_field = unit.get(field_name)
        normalized = normalize_field(raw_field)
        is_fact = normalized["status"] == FACT

        satisfied = False
        if is_fact:
            comparator = _OPS.get(op)
            value = normalized["value"]
            if comparator is None or value is None:
                is_fact = False  # cannot compare -> unresolved, not a guess
            else:
                try:
                    satisfied = bool(comparator(value, target))
                except TypeError:
                    is_fact = False  # incomparable types -> unresolved

        if satisfied:
            results[gate_name] = GATE_PASS
        elif is_fact:
            results[gate_name] = GATE_FAIL if mode == "hard" else GATE_UNKNOWN
        else:
            results[gate_name] = GATE_UNKNOWN
    return results


def verify_checklist(gate_results: dict[str, str], unit: dict[str, Any]) -> list[str]:
    """One "confirm X before contacting or touring" string per UNKNOWN gate
    in gate_results, each carrying the unit's deep link when one exists.

    gate_results is expected to hold hard-mode gates only (see the module
    docstring) - every UNKNOWN entry here becomes a checklist item.
    """
    deep_link = unit.get("unit_deep_link") or unit.get("verify_url") or unit.get("url")
    items: list[str] = []
    for gate_name, result in gate_results.items():
        if result != GATE_UNKNOWN:
            continue
        label = gate_name.replace("_", " ")
        if deep_link:
            items.append(f"Confirm {label} before contacting or touring: {deep_link}")
        else:
            items.append(f"Confirm {label} before contacting or touring (no deep link on file)")
    return items


def tour_now_blocked(gate_results: dict[str, str]) -> bool:
    """True if any gate is UNKNOWN or FAIL - the unit caps at "Verify
    first" and cannot reach TourNow. (FAIL units are normally excluded
    upstream already; this still blocks them defensively.)"""
    return any(result in (GATE_UNKNOWN, GATE_FAIL) for result in gate_results.values())


# ---------------------------------------------------------------------------
# Constraint grading (structurally-checkable subset of constraints.json)
# ---------------------------------------------------------------------------

PLACEHOLDER_PATTERNS = [
    re.compile(r"\bTODO\b"),
    re.compile(r"\bTBD\b"),
    re.compile(r"\bPLACEHOLDER\b", re.IGNORECASE),
    re.compile(r"\bFIXME\b"),
    re.compile(r"\bXXX\b"),
    re.compile(r"lorem ipsum", re.IGNORECASE),
    re.compile(r"example\.(com|org|net)", re.IGNORECASE),
]

RULE_VALUE_WITHOUT_PROVENANCE = "value-without-provenance"
RULE_PLACEHOLDER_LEFT_IN_OUTPUT = "placeholder-left-in-output"
RULE_GRADE_WITHOUT_SOURCE = "grade-without-source"
RULE_PRICE_NOT_NUMBER = "price-not-number"
RULE_UNVERIFIED_MARKED_LIVE = "unverified-marked-live"


def _walk_placeholders(obj: Any, path: str = "") -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if isinstance(obj, str):
        for pattern in PLACEHOLDER_PATTERNS:
            match = pattern.search(obj)
            if match:
                findings.append({
                    "rule_id": RULE_PLACEHOLDER_LEFT_IN_OUTPUT,
                    "path": path or "$",
                    "detail": f"matched {match.group(0)!r} in {obj!r}",
                })
                break
    elif isinstance(obj, dict):
        for key, value in obj.items():
            findings.extend(_walk_placeholders(value, f"{path}.{key}" if path else str(key)))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            findings.extend(_walk_placeholders(value, f"{path}[{index}]"))
    return findings


def _check_record_rules(record: dict[str, Any], path: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []

    for key, value in record.items():
        field_path = f"{path}.{key}" if path else str(key)

        if isinstance(value, dict) and "status" in value:
            status = value.get("status")
            field_val = value.get("value")
            source = value.get("source")
            if status in (FACT, INFERRED) and field_val is not None and not source:
                findings.append({
                    "rule_id": RULE_VALUE_WITHOUT_PROVENANCE,
                    "path": field_path,
                    "detail": f"status={status} value={field_val!r} has no source",
                })

        if isinstance(key, str) and key.lower() == "grade" and value not in (None, ""):
            sources = record.get("sources") or record.get("source")
            if not sources:
                findings.append({
                    "rule_id": RULE_GRADE_WITHOUT_SOURCE,
                    "path": field_path,
                    "detail": f"grade={value!r} has no sibling sources",
                })

        if isinstance(key, str) and ("price" in key.lower() or "rent" in key.lower()):
            # A price/rent field may be a bare legacy scalar or a provenance
            # dict ({value, status, source, evidence, confidence}); check the
            # actual carried value either way, not just the raw dict.
            price_candidate = value
            if isinstance(value, dict) and "status" in value:
                price_candidate = field_value(value)
            if isinstance(price_candidate, str):
                findings.append({
                    "rule_id": RULE_PRICE_NOT_NUMBER,
                    "path": field_path,
                    "detail": f"{key}={price_candidate!r} is a string, must be a plain number",
                })

    if record.get("live") is True:
        verified_at = record.get("verified_at")
        evidence = record.get("screenshot") or record.get("unit_deep_link")
        if not verified_at or not evidence:
            findings.append({
                "rule_id": RULE_UNVERIFIED_MARKED_LIVE,
                "path": path or "$",
                "detail": "live=true without verified_at and screenshot/unit_deep_link evidence",
            })

    return findings


def grade_fields(data: Any, constraints: dict[str, Any] | None = None) -> dict[str, Any]:
    """Grade a produced document against the structurally-checkable subset
    of constraints.json's autofail rules.

    `data` may be a single unit dict, a dict of units keyed by id, or any
    nested JSON-like structure - every nested dict is visited as a
    candidate record for the record-shaped rules; every string anywhere is
    scanned for placeholder patterns.

    If `constraints` (a parsed constraints.json) is given, only rule ids
    marked autofail there are reported; otherwise all rules this function
    knows how to check are reported.

    Returns {"fields_graded": <number of dict records visited>,
    "autofails": [{"rule_id", "path", "detail"}, ...]}.
    """
    active_ids = None
    if constraints is not None:
        active_ids = {
            rule["id"] for rule in constraints.get("rules", []) if rule.get("autofail")
        }

    autofails: list[dict[str, str]] = []
    fields_graded = 0

    def visit(obj: Any, path: str) -> None:
        nonlocal fields_graded
        if isinstance(obj, dict):
            fields_graded += 1
            autofails.extend(_check_record_rules(obj, path))
            for key, value in obj.items():
                visit(value, f"{path}.{key}" if path else str(key))
        elif isinstance(obj, list):
            for index, value in enumerate(obj):
                visit(value, f"{path}[{index}]")

    visit(data, "")
    autofails.extend(_walk_placeholders(data))

    if active_ids is not None:
        autofails = [a for a in autofails if a["rule_id"] in active_ids]

    return {"fields_graded": fields_graded, "autofails": autofails}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) not in (2, 3):
        print("usage: gates.py unit.json config_gates.json [constraints.json]", file=sys.stderr)
        return 2

    unit = json.loads(pathlib.Path(argv[0]).read_text())
    config_gates = json.loads(pathlib.Path(argv[1]).read_text())

    gate_results = evaluate_gates(config_gates, unit)
    checklist = verify_checklist(gate_results, unit)
    blocked = tour_now_blocked(gate_results)

    out: dict[str, Any] = {
        "gates": gate_results,
        "verify_checklist": checklist,
        "tour_now_blocked": blocked,
    }
    if len(argv) == 3:
        constraints = json.loads(pathlib.Path(argv[2]).read_text())
        out["grading"] = grade_fields(unit, constraints)

    json.dump(out, sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
