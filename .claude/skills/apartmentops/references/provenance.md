# Provenance, gates, and anti-fabrication constraints

This is the scan/verify truth layer's contract. It covers three things that
compose into one pipeline:

1. The field-level provenance shape every extracted value should carry.
2. The tri-state hard-gate evaluation built on top of that shape.
3. The machine-readable anti-fabrication constraints file and how it
   propagates into subagent prompts and a grading pass.

The Python implementing (1) and (2) is `../scripts/gates.py`
(`field_value`, `field_status`, `normalize_field`, `evaluate_gates`,
`verify_checklist`, `tour_now_blocked`, `grade_fields`). The embedded-JSON
extraction that feeds provenance's `source`/`evidence` is
`../scripts/extract_embedded.py`. Read both module docstrings for exact
function signatures - this file explains the contract, not the API.

## 1. Field-level provenance shape

Every field on a unit (or an area, or anything else `references/contracts.md`
describes) should be either a bare legacy scalar (pre-existing data, treated
as FACT with source null if non-null, MISSING if null) or this dict:

```json
{
  "value": 2450,
  "status": "FACT",
  "source": "https://platform.example/unit/123",
  "evidence": "apartmentops/shots/unit-123.png",
  "confidence": null
}
```

### Status rules

- **FACT** - value confirmed. Requires `source` (a URL) and `evidence` (a
  screenshot path or a verbatim quote). A FACT with no source is a
  fabrication (`value-without-provenance` below), not a fact.
- **INFERRED** - value reasoned rather than directly observed. Requires
  `confidence` (0.0-1.0) and expects `evidence` to carry the reasoning. A
  confidence **below 0.3** discards the value entirely: `normalize_field()`
  rewrites the field to `{"value": null, "status": "MISSING", ...}` rather
  than keeping the weak guess as a hedge. Confidence exactly 0.3 stays
  INFERRED.
- **MISSING** - `value` is null. Never guessed, never demo-substituted,
  never carried forward from a previous run's value.
- **CONFLICT** - sources disagree. `value` is null (no silent winner); the
  competing observations live in a `conflicts` list of
  `{value, source, evidence}` so a human can adjudicate. Status stays
  CONFLICT - it is not downgraded.

`extraction_method` (e.g. `"embedded:next_data"`, `"embedded:ld_json"`,
`"dom"`) folds into a FACT/INFERRED field's `evidence` or as an extra key
alongside it - it names *how* the value was obtained, on top of *where*
(`source`) and *proof* (`evidence`/screenshot).

### Migration

An existing verified.json written before this schema existed has bare
scalars. Per field: a value backed by a deep link and a screenshot becomes
FACT citing them; anything else becomes MISSING. Concretely, call
`gates.normalize_field(raw_value)` per field - the legacy-scalar branch
already implements exactly this rule (non-null -> FACT/source-null,
null -> MISSING). Do not invent a confidence or a source during migration;
an untraceable legacy value is MISSING, not a weak FACT.

## 2. Tri-state hard gates

A hard gate (max rent, minimum beds, in-unit laundry, ...) evaluates
three-valued instead of binary:

- **PASS** - a FACT satisfies the gate.
- **FAIL** - a FACT violates the gate. The unit is excluded, same as before
  this epic.
- **UNKNOWN** - the backing field is INFERRED, MISSING, or CONFLICT (a gate
  needs a FACT; inference at *any* confidence still maps to UNKNOWN here -
  it may still feed a scoring bonus elsewhere, just not a hard gate).

Bonus-mode gates (e.g. `floor_min`, `sqft_min` per `config.yml`'s
`mode: bonus`) never produce FAIL - an unmet or unresolved bonus criterion
reads UNKNOWN and is expected to be scored elsewhere (the 7-dimension
rubric), not to block a unit.

`evaluate_gates(config_gates, unit)` (see its docstring for the
`config_gates` shape) returns `{gate_name: "PASS"|"FAIL"|"UNKNOWN"}`.
**Build the `gates` block that ends up in verified.json, and that feeds
`verify_checklist`/`tour_now_blocked`, from hard-mode gates only.**
Evaluate bonus-mode gates with a separate call if you need their PASS/
UNKNOWN status for scoring - they should not appear in the tri-state gates
block, since `verify_checklist`/`tour_now_blocked` treat every key they are
given as gating TourNow.

### config.yml -> config_gates translation (worked example)

`evaluate_gates` intentionally does not parse `config.yml`'s `gates:` block
itself (it mixes hard/bonus modes, preference lists, and blocklists in
ways specific to that file). The scan/verify integration should build the
generic `config_gates` dict `evaluate_gates` expects, e.g.:

| config.yml entry | unit field it reads | config_gates entry |
|---|---|---|
| `unit.baths: {value: 2, mode: hard}` | `baths` | `{"field": "baths", "op": "gte", "value": 2, "mode": "hard"}` |
| `gates.in_unit_laundry: {value: true, mode: hard}` | `in_unit_laundry` | `{"field": "in_unit_laundry", "op": "eq", "value": true, "mode": "hard"}` |
| `gates.floor_min: {value: 10, mode: bonus}` | `floor` | `{"field": "floor", "op": "gte", "value": 10, "mode": "bonus"}` |
| `gates.exposure_blocked: [N]` (non-empty) | `exp` via `gates.exp_gate_field` | `{"field": "exp", "op": "not_in", "value": ["N"], "mode": "hard"}` |
| `budget.gross_max: 5000` | `rent_verified` | `{"field": "rent_verified", "op": "lte", "value": 5000, "mode": "hard"}` |

`exposure_preferred` and an empty `exposure_blocked` are not hard gates -
they are preference inputs to scoring, not translated into `config_gates`
at all.

Orientation has exactly one canonical spelling on a verified unit: the `exp` object `{dir, south, conf, src}` defined in `references/contracts.md`.
`evaluate_gates` does a flat `unit.get(field)` and would read a bare `exp.dir` string as a FACT even when `conf` is LOW or UNK, so an exposure gate never reads `exp` directly.
The caller derives the gate's field with `gates.exp_gate_field(unit["exp"])` and evaluates against a copy of the unit where `exp` is replaced by that provenance object: `conf: HIGH` becomes a FACT (value `dir`, source and evidence `src`), `MED` / `LOW` become INFERRED (confidence 0.6 / 0.3, which the gate reads as UNKNOWN), and `UNK`, a null `dir`, or a missing `exp` become MISSING.
The scan phase's `light_view` string is raw input only; it is normalized into `exp` before anything downstream reads it.

### Verify-before-tour checklist and the TourNow band

`verify_checklist(gate_results, unit)` produces one
`"Confirm <gate> before contacting or touring: <deep link>"` string per
UNKNOWN gate. `tour_now_blocked(gate_results)` is `True` if any gate is
UNKNOWN or FAIL. The band rule is absolute: **no unit reaches TourNow while
`tour_now_blocked` is True** - it caps at "Verify first" (naming the
blocking gates) until every hard gate is a FACT-backed PASS. Re-run
`evaluate_gates` on hydrate so a field that resolves to a satisfying FACT
can promote a unit into TourNow on a later run.

## 3. Anti-fabrication constraints and the grading pass

`../references/constraints.json` is the machine-readable rule set
(`{version, rules: [{id, description, autofail}]}`). It propagates two
ways:

1. **Prompt injection** - every scan subagent prompt (in
   `apartmentops-scan/SKILL.md`'s fan-out) should include the file's rules
   verbatim, so anti-fabrication is enforced by instruction, not left
   implicit.
2. **Grading pass** - after a scan run produces output, grade it against
   the same file. `gates.grade_fields(data, constraints)` implements the
   structurally-checkable subset:
   - `value-without-provenance` - any dict with a `status` of FACT/INFERRED,
     a non-null `value`, and no `source`.
   - `placeholder-left-in-output` - any string anywhere in the structure
     matching a placeholder pattern (TODO, TBD, PLACEHOLDER, FIXME, XXX,
     lorem ipsum, example.com/.org/.net).
   - `grade-without-source` - a `grade` key with a non-empty value and no
     sibling `sources`/`source` key (matches `areas.json`'s
     `{"grade": ..., "sources": [...]}` shape from `contracts.md`).
   - `price-not-number` - any key containing `price` or `rent` whose value
     is a string instead of a plain number.
   - `unverified-marked-live` - `"live": true` with no `verified_at` and no
     `screenshot`/`unit_deep_link` evidence.

   `grade_fields` returns `{"fields_graded": <count>, "autofails": [...]}`.
   Call it once per unit (or once over the whole verified.json document -
   both work; `fields_graded` counts dict records visited either way) to
   build `apartmentops/data/grading.json`
   (`{constraints_version, fields_graded, autofails, withheld_units}` is a
   reasonable shape - the withheld-unit bookkeeping itself belongs to the
   scan integration, since it needs to know which unit a finding's `path`
   belongs to).

   A unit with any autofail hit should be withheld from verified.json and
   listed in the run report with its rule id(s) and offending field(s);
   withheld units are re-verified next run rather than dropped for good.

`grade_fields` is a pure, read-only, no-network function - it never sends
anything and never modifies its input.
