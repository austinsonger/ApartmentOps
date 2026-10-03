# Cost engine (costs.py)

Every dollar figure ApartmentOps shows a user comes from
`.claude/skills/apartmentops/scripts/costs.py`, never from an LLM doing
arithmetic in a prompt. The engine is a small set of pure, type-hinted,
pytest-covered Python functions plus a thin CLI:

```
python3 .claude/skills/apartmentops/scripts/costs.py inputs.json > apartmentops/data/results.json

# Optional local income comparison - a CLI flag, never a line in
# inputs.json or results.json (see "Income never touches a committed
# file" below):
python3 .claude/skills/apartmentops/scripts/costs.py inputs.json --income 180000 > apartmentops/data/results.json
```

**The rule: dashboard numbers come only from results.json.** A skill or
the dashboard renderer reads results.json and narrates it in prose - it
never recomputes a fee, a total, or a ranking itself. If a number is not
in results.json, it is rendered as `MISSING` or `n/a`, never estimated on
the spot. This is what makes every dollar figure reproducible: the same
`inputs.json` always produces a byte-identical `results.json` (no
timestamp field is ever written to it).

See the module docstring in `costs.py` for the exact `inputs.json` /
`results.json` shapes and every function's signature and return shape.
This file covers the things that are policy, not code: the declared
assumption stack, the income guardrail, and the rent-control resolution
procedure.

## What the renter actually pays

A net-effective rent is the gross rent with a concession averaged over the
whole term, so it is an average, not a price anyone is billed: the monthly
check is the gross, and the concession arrives as a credit in specific months
(often the first or last). Renewal offers start from the gross, never from
the net-effective figure, so every note that states a net figure writes the
gross beside it ("net $X / gross $Y"), and `quality.net_without_gross_hits`
sweeps for notes that do not.

## The declared assumption stack

Every unit in one engine run is compared against the same uniform
assumptions - per-unit ad-hoc assumptions are not allowed, or a ranking
would stop comparing units like with like. Defaults live in
`costs.DEFAULT_ASSUMPTIONS`; a run can override any of them via
`inputs.json`'s top-level `"assumptions"` object.

| Key | Default | Meaning |
|---|---|---|
| `lease_term_months` | 12 | Lease length used when a unit does not state its own `term_months`. |
| `renewal_increase_pct` | 3.0 | The declared year-2 rent increase for a building with no cited rent-control cap (status `exempt` or `UNKNOWN`). This is a stated assumption, not a market forecast - it should read to the user as "we assumed X%", never as a prediction. |
| `deposit_float_rate_pct` | 5.0 | Annualized opportunity-cost rate for a tied-up security deposit, used by `deposit_float_fee()` to produce a one-time fee line for `amortized_one_time()` / `true_monthly_cost()`. |
| `qualification_multiple` | 40 | The income multiple used by `qualification()` (the standard "40x rent" landlord screening rule) unless a unit calls it with a different multiple. |

Every `results.json` echoes the exact assumption values used for that run
in its top-level `"assumptions"` block, so nothing an LLM narrates is ever
guessed from memory.

**No market forecasting.** `renewal_increase_pct` and any other increase
assumption are declared inputs the user (or the module default) chooses,
never a prediction the engine makes about where rents are headed. Where a
real cap exists (a building is actually rent-controlled), that cap - not
the declared assumption - governs. That is the rent-control resolution
procedure below.

## Income never touches a committed file

`qualification()` computes `required_income` from rent alone (rent times
`qualification_multiple`) - that figure needs no personal data and is
always present. A local annual income is optional and, if the user wants
to see a `qualifies` boolean, is supplied at run time only:

- **Library:** `build_results(data, income_annual=180000)` - a keyword
  argument, never a field read off `data`.
- **CLI:** `costs.py inputs.json --income 180000` - a flag, never a line
  in `inputs.json`.

`validate_inputs()` raises if `inputs.json` itself contains an
`"income_annual"` key, so the on-disk file that a build step assembles
from `config.yml` / `verified.json` / `transit.json` cannot carry income
even by accident. `qualification()`'s return value carries only
`required_income` and `qualifies` - deliberately no income/gross ratio,
because a ratio times the (already-present) gross figure would
reconstruct the income figure it was computed from. Nothing this module
returns ever reveals the income figure, directly or by arithmetic.

## Rent-control resolution procedure

`year2_gross()`, `renewal_vs_relocate()`, and `rank_by_tco24()` all accept
a `rent_control` argument shaped:

```json
{
  "status": "controlled",
  "cap_pct": 3.0,
  "source": "https://... (cited ordinance section and/or tax record URL)"
}
```

`status` is one of `controlled`, `exempt`, or `UNKNOWN`. This module never
infers, guesses, or defaults a status from a heuristic (building age,
unit count, neighborhood, or anything else) - it only ever consumes a
status that has already been resolved and cited elsewhere, or treats a
missing/`UNKNOWN` status as `UNKNOWN`. `controlled` additionally requires
a numeric `cap_pct`; if the caller passes `status: "controlled"` without
one, `year2_gross()` raises rather than guessing a number.

**Resolution itself - who determines `status` and `cap_pct` for a given
building - is a research-stage responsibility, not this module's.** The
declared design (owned by the research stage, per the epic that lands
this file) is:

1. Resolve rent-control applicability **once per building**, from public
   ordinance text and, where relevant, tax records - construction date,
   unit count, and new-construction exemptions are the kinds of facts
   that typically settle it, but which facts actually settle it is
   jurisdiction-specific and must be discovered empirically per
   jurisdiction, never assumed in advance.
2. Store the result in that building's knowledge file
   (`apartmentops/data/buildings/{slug}.json`, or wherever the research
   stage's schema places it) as:
   ```json
   {
     "rent_control_status": "controlled",
     "basis": "construction_year",
     "cap_rule": "the annual increase formula as stated by the ordinance, verbatim or closely paraphrased",
     "source": ["https://... ordinance section", "https://... tax record"],
     "resolved_date": "2026-07-15"
   }
   ```
3. If nothing in the public record settles applicability, or the record
   could not be found or read, `rent_control_status` is written as
   `UNKNOWN` with the reason - never guessed, never left to default to
   `exempt` or `controlled`.
4. Translating a text `cap_rule` (for example "greater of 3.75% or CPI,
   whichever is lower") into the numeric `cap_pct` this module expects for
   a specific run is a decision made by whoever assembles that unit's
   engine input for that run. That translation must be documented next to
   its citation at the point it is made - this module receives only the
   resulting number and does not re-derive it.
5. Record resolution is read-only browsing of public pages, user-initiated,
   never on a schedule, with no logins and no bot-wall or CAPTCHA bypass -
   the same guardrails as every other browsing surface in ApartmentOps.

**Jurisdictions such as Hoboken and Jersey City are examples of the class
of municipality that has a rent-control ordinance with building-level
applicability rules - this file does not assert their current cap values,
exemption thresholds, or applicability tests.** Those are exactly the
kind of fact that drifts (ordinances get amended, exemption thresholds
change) and must always be re-resolved from a live, cited source at
resolution time, never hardcoded into a skill, a script, or this
reference file. Any `cap_pct` or `status` this module ever sees must trace
back to a `source` citation and a `resolved_date` in a building knowledge
file, or it must be `UNKNOWN`.

## LLM narrates, never computes

No skill, prompt, or dashboard-rendering step should ever add, multiply,
amortize, or otherwise compute a dollar figure that could instead be read
from `results.json`. If a number a user needs is not present there, the
correct response is to say so (`MISSING`, `n/a`, or "run the cost
engine") and, if appropriate, note that the cost engine needs to be run
or re-run - not to approximate it in prose. This is the whole point of
having a deterministic engine: the same inputs always produce the same
numbers, and every number is regression-tested.
