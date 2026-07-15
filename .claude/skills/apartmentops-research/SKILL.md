---
name: apartmentops-research
description: >-
  ApartmentOps stage 3 - grade the neighborhoods and price the commute. For
  every area that appears in apartmentops/data/verified.json, researches
  safety (crime statistics), cleanliness (sanitation and rodent-complaint
  data, resident sentiment), nearby essentials with walk times, and computes
  door-to-door transit time and monthly fare cost to the user's anchor.
  Writes apartmentops/data/areas.json and transit.json. Use when the user
  asks "how safe is this area", "what is the commute really like", "how
  clean are these neighborhoods", or after a scan produces units in areas
  that have not been graded yet.
---

# ApartmentOps: Research (safety, cleanliness, transit)

Inputs: `apartmentops/config.yml`, `apartmentops/scoring.yml`,
`apartmentops/data/verified.json`, `apartmentops/data/snapshots.jsonl`.
Outputs: `apartmentops/data/areas.json`, `apartmentops/data/transit.json`,
`apartmentops/data/buildings.json`, `apartmentops/data/scores.json`,
`apartmentops/data/lines.json`. Schemas in
`../apartmentops/references/contracts.md`. Only research areas that
actually contain live units - do not grade the whole city; the same
scoping principle carries into buildings, scoring, and the distress/climate
and line-advisor passes below - enrich and score what is actually live,
not the whole market.

## Area grading (one researcher per area, parallel)

Each area researcher must return, with a URL cited for every claim:

1. **Safety** - violent and property crime vs the city and national
   averages, from published crime-statistics sources (police precinct or
   department data, established crime-grading sites, local news). Produce a
   letter grade (A-F, plus/minus allowed) with a one-line justification
   containing actual numbers. Capture block-level nuance when it exists: the
   doormanned tower block and the strip two streets over can grade
   differently, and saying so is more useful than an average.
2. **Cleanliness** - sanitation scorecards and rodent/litter complaint data
   where the city publishes them (e.g. NYC 311 and rat-map data), resident
   sentiment otherwise. Letter grade + justification. Note structural
   factors (flood-prone blocks, construction dust) honestly.
3. **Essentials** - the 4-8 nearest groceries, pharmacies, parks, gyms, and
   transit entrances with realistic walk minutes.
4. **Vibe** - one or two honest sentences, including the risks the listing
   sites will not mention.

Rules: grades must trace to sources; if data is thin, grade what you can
and say what you could not. If an area was not researched, omit it entirely -
the dashboard shows "n/a", and an honest n/a beats an invented B.

## Transit (one analyst)

For the anchor in config (skip if null): compute weekday peak door-to-door
transit from each area to the anchor as walk-to-station + typical wait +
ride + final walk. Two traps to call out explicitly:

- Use REAL station walk times from the map, not marketing copy.
- Fare systems differ: check current fares (they change yearly), whether
  transfers between systems are free (often they are not - stacked fares
  can double a monthly cost), and compute the monthly cost at ~42
  rides/month using the cheapest sensible option (pass vs pay-per-ride).

Write transit.json with per-area typical minutes, range, route description,
and monthly cost, plus the fare table with source URLs.

## Building enrichment

For every building that appears in `apartmentops/data/verified.json`, write
(or refresh) one entry in `apartmentops/data/buildings.json`, keyed by the
building slug (schema: `../apartmentops/references/contracts.md`). Resolve
once and cache - a building that already carries a resolved `flood` block
or a non-`UNKNOWN` `rent_control.status` does not need re-resolving every
run, since neither fact moves week to week.

**Flood.** Geocode the building's address to lat/lon with the bundled
`../apartmentops/scripts/geocode.py` (Nominatim, polite rate limit built
in - the same script onboarding uses for the anchor); if the lookup fails,
leave `lat`/`lon` null and skip the flood call rather than guessing
coordinates. Then call `../apartmentops/scripts/flood.py`'s `flood_zone(lat,
lon, cache_path="apartmentops/data/cache/flood.json")` once per building -
the cache means a repeat run reads the prior result instead of re-querying
FEMA. Write whatever it returns straight into the `flood` block: on
success, `zone`/`subtype`/`sfha`/`source_url`/`viewer_url`/`fetched_at`; on
any failure (missing coordinates, network error, no mapped polygon at that
point) `flood_zone()` itself returns `{zone: null, error, source_url,
viewer_url, fetched_at}` - write that shape verbatim so the dashboard chip
renders "n/a" and never a fabricated risk level. `viewer_url` is what the
chip links to.

**Rent control.** Follow the resolution procedure in
`../apartmentops/references/costs.md`: resolve applicability ONCE per
building from public ordinance text and, where relevant, tax records -
construction date, unit count, and new-construction exemptions typically
settle it, but confirm empirically per jurisdiction rather than assuming.
Write `{status: "controlled"|"exempt"|"UNKNOWN", cap_pct, source,
resolved_at}` into the `rent_control` block exactly per `contracts.md`'s
schema. `cap_pct` is only ever a number when `status` is `controlled`, and
it must trace to the cited `source`; if the ordinance states a formula
("greater of 3.75% or CPI, whichever is lower") rather than a flat number,
translate it to the numeric `cap_pct` this run should use and document that
translation next to the citation in your run notes, since the JSON field
itself carries only the number. If nothing in the public record settles
applicability, or the record could not be found or read, write `status:
"UNKNOWN"` and say why in the run report - never default to `exempt` or
`controlled`, and never leave the building unresolved silently; an
`UNKNOWN` entry still gets written, so the dashboard shows "rent control:
unknown" instead of nothing.

## Scoring

Score every live unit in `verified.json` against the committed rubric with
`../apartmentops/scripts/scoring.py`. Load it once via
`load_spec("apartmentops/scoring.yml")` (created at onboarding from
`examples/scoring.example.yml`; full usage rules in
`../apartmentops/references/scoring.md`).

Build each unit's `dim_scores = {dimension: raw_score | None}`:

- `commute` / `safety` / `cleanliness` - hand whichever researcher produces
  the underlying signal (the Transit analyst's door-to-door minutes, the
  Area grading researcher's letter grade) that dimension's anchor table
  from `scoring.yml` verbatim, and require a number PLUS the anchor band it
  says the number falls in; a number that does not match its cited band is
  rejected and retried, never accepted as-is (`scoring.md` section 1).
  This is the same anchor-constrained pattern the Area grading and Transit
  sections above already gather raw material for - do not let a subagent
  invent its own free-form scale.
- `budget_fit` - pure arithmetic against `config.yml`'s `budget` block
  (`target`, `gross_max`, `gross_max_stretch`, `allow_net_effective`) and
  the unit's own verified rent (prefer a cost-engine `results.json` total
  if one has been run for the unit, else `rent_verified`/`rent_net`); map
  the comparison straight onto the four anchor meanings already committed
  in `scoring.yml` - no subjective judgment needed here.
- `move_in_window_fit` - call `move_in_fit_score(available_date,
  window_start, window_end, spec)` directly. Read the window from
  `config.yml`'s `move_in_target`; since that is a single date rather than
  a range, use `window_start == window_end == move_in_target`. If
  `move_in_target` is null, do not call it at all - the dimension is simply
  absent from `dim_scores`, and `score_unit`'s renormalization handles the
  rest.

Then `score_unit(dim_scores, spec)` gives `{composite, band, renormalized,
renormalization_note}` - a missing dimension's weight is redistributed
across the rest, never silently dropped from the record (`scoring.md`
section 2). Gate it: read the unit's own `gates` field from `verified.json`
(the hard-mode gates the scan stage already evaluated) and call
`gates.tour_now_blocked(unit["gates"])`; feed that into `band_with_gates(
band, tour_now_blocked)` so a unit with any `UNKNOWN` hard gate is demoted
out of TourNow regardless of composite score.

Once every live unit has a post-gate band, run `calibrate(bands, spec)`
over the whole batch. If `ok` is `False`, surface `warning` verbatim in the
run report; never adjust `action_bands.tour_now_min` yourself - that
threshold change is a human edit to `scoring.yml` (`scoring.md` section 3).

Write `apartmentops/data/scores.json` per `contracts.md`: one entry per
`unit_id` with `composite`, `band` (post-gate), `renormalized`,
`renormalization_note`, `dims` (the raw per-dimension scores you fed into
`dim_scores`, not fractions), and `distress` (next section).

## Market distress and concession climate

Requires snapshot history - a unit or area with fewer than two
`snapshots.jsonl` runs has no basis for any of this; say so and move on,
never treat an empty result as "no distress observed"
(`../apartmentops/references/ledger.md`, `scoring.md` section 5).

For every live unit, pull `../apartmentops/scripts/snapshots.py`'s
`trajectory(path, unit_id)` - the ordered `[{run_id, observed_at, price,
status}]` series - and derive the `events` dict `distress_flags(events,
spec)` expects:

- `relist_count` - rows where `status == "live"` and the immediately
  preceding row (by `observed_at`) was `status == "gone"`; the unit's
  first-ever observation never counts as a relist.
- `cuts_30d` - consecutive-observation price decreases (both prices
  non-null) whose later row's `observed_at` falls within the trailing 30
  days of the unit's most recent observation.
- `days_listed` - days from the start of the unit's CURRENT listing stint
  (its most recent gone-to-live transition, or its first observation if it
  has never gone) to its most recent observation.
- `neighborhood_median_days` - the median `days_listed` (computed the same
  way) across every other live unit in the same `area`; compute this once
  per area per run, not once per unit.

A unit with fewer than 2 trajectory rows has no basis for any of these
fields - leave them `None` rather than guessing zero, and note "no history
yet" wherever the flags render, since an empty flag list and "never
checked" are different claims. Call `distress_flags(events, spec)` and
write the returned list into that unit's `distress` field in `scores.json`.
Flags are framed as negotiation leverage for the renter, never as scam
language - that is Block H's job, not this one.

For concession climate, assemble `stats = {tracked, with_cuts,
median_days_live, concession_mentions}` from the same ledger history, once
per area (and per building where volume supports it), and call
`concession_climate(stats, spec)`. Write the result into that area's
`climate` field in `apartmentops/data/areas.json` exactly per
`contracts.md`: `sample_size` is ALWAYS present on the output; below
`scoring.yml`'s `climate.min_sample` the classification is `null` and the
chip renders "insufficient sample (n=X)", never a guessed label.

Optionally attach a FRED regional rent-CPI figure as cited backdrop prose
next to the climate block - a plain keyless GET against FRED's public
series endpoint, citing the series id and date. This is context only;
never blend it into `concession_climate()`'s own classification math,
which reads ledger signals alone.

## Negotiation anchor

For any unit whose area or building climate resolved to a real (non-
insufficient-sample) classification, compute a concrete ask with
`negotiation_ask(unit, climate, comps)`:

- `unit`: `{cuts: <price cuts observed for this unit>, days_listed: <from
  the distress events above>, current_price: <the unit's `rent_verified`>}`
- `climate`: the unit's area (or building) `concession_climate()` result
- `comps`: optional `[{note, price}]` - same-line siblings from the line
  advisor below, or same-building comps; only a comp strictly cheaper than
  `current_price` gets cited

The result - `{ask: {weeks_free, monthly_off}, justification}` - goes into
the unit's tour dossier and the printable one-pager. THIS IS A NUMBER THE
USER SAYS IN PERSON OR TYPES INTO AN APPLICATION THEMSELVES. Nothing here
drafts, sends, or submits an offer, an email, or a message to a landlord or
broker - `negotiation_ask()` only computes the evidence-linked number, and
every string in `justification` traces back to the `unit`/`climate`/`comps`
values passed in, nothing invented. When `insufficient_data` comes back
true, render the `missing` list and stop - never fall back to a guessed
ask.

## Line advisor

For each building with a cited line map at
`apartmentops/data/lines/{slug}.yml` (schema and the trailing-letter trust
rule: `../apartmentops/references/line-substitution.md` - hand-curated from
an official floor plan or key plan URL, never inferred from a unit
number's trailing letter alone), run
`../apartmentops/scripts/line_advisor.py`'s `build_lines_index(
verified_units, line_maps_by_building, trajectories)` over the batch.
`line_maps_by_building` comes from `load_all_line_maps(
"apartmentops/data/lines/")`, keyed by each file's own `building` field -
keep that field byte-identical to `verified.json`'s `building` string or
the join silently fails for that building's units. `trajectories` is the
same `{unit_id: [...]}` shape `snapshots.trajectory()` produces - build it
once for the batch by calling `trajectory()` per candidate `unit_id`.

Write the result as `apartmentops/data/lines.json`: `{"lines": index,
"report": report}`, matching what `line_advisor.py`'s own CLI emits. A
building with no line map, or a unit whose number does not resolve to a
confirmed line, is silently absent from `index` - never a guessed sibling
list; the `report` counts (`no_line_map`, `unparsed_unit`) make those
exclusions visible without inventing data for them.

Surface each unit's `siblings` (cheaper live same-line units, with floor
and price deltas) and `line_history` (times listed, price range across the
trailing 12 months) in its tour dossier. When `negotiation_ask()` above
wants a comp, the cheapest live sibling is the natural candidate - pass its
price as `price` and a short line like "same-line unit `<unit_id>`" as
`note`.

## Wrap up

Summarize the grades in a compact table, flag any area whose grade should
change how the user weighs its units (a bargain in a C area is a different
product than the same price in an A area), and offer the next stage:
`apartmentops-dashboard`.

Also call out: any unit whose composite would have hit TourNow but was
demoted by an `UNKNOWN` hard gate (name the gate), any `calibrate()`
warning, any building whose `flood` or `rent_control` is still `n/a` /
`UNKNOWN` (say why), and the single strongest negotiation anchor in the
batch if one exists. These are the lines a user skims first.
