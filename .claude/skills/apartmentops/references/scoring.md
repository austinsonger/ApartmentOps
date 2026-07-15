# Scoring spec usage (scoring.yml)

`apartmentops/scoring.yml` (example at `examples/scoring.example.yml`) is
the committed rubric every unit is scored against. It exists so that scores
mean the same thing across runs and across research subagents, instead of
drifting with each model's free-form numeric judgment. The library that
reads it is `.claude/skills/apartmentops/scripts/scoring.py`:
`load_spec`, `score_unit`, `band_with_gates`, `calibrate`,
`distress_flags`, `move_in_fit_score`, `concession_climate`,
`negotiation_ask`.

Nothing in this file or in scoring.py sends, drafts-and-sends, or submits
anything to anyone. Every number described below - the composite, the
action band, the distress flags, the climate classification, the
negotiation ask - is a value the user reads, evaluates, and if they choose,
says out loud in a leasing office or types themselves.

## 1. Verbal anchors constrain subagent output

Each dimension in `scoring.yml` declares `weight`, `anchors` (a list of
`{score_min, score_max, meaning}`), and `rank_based`. A research subagent
scoring, say, `commute` is handed that dimension's anchor table verbatim
and must return a number **and** the anchor band it says that number falls
in. If the number and the cited band disagree, the output is rejected and
the subagent retries - the rubric enforces itself instead of relying on
prompt discipline. A dimension with no groundable evidence is `null`
(never a guessed midpoint) and is handled by renormalization, below.

`load_spec()` validates the committed file before anything is scored:
weights across all dimensions must sum to 1.0 (tolerance 1e-6), and every
dimension's anchor bands must not overlap and must not leave a gap greater
than 1 between adjacent integer bounds. That "gap of at most 1" tolerance
is deliberate, not a bug: it is what lets whole-number bands tile an
integer range with clean, non-overlapping text like `13-16` / `17-20` -
every *integer* in the dimension's range still lands in exactly one band.
A spec that fails either check raises `ValueError` at load time rather than
producing silently-wrong composites. See section 2's note on fractional
scores for how `score_unit` handles a raw value that lands inside one of
those permitted integer gaps.

Anchors are always ordered so that a **higher raw score means a more
favorable outcome** for that dimension - this is what lets `score_unit`
treat every dimension the same way when combining them.

## 2. Composite and the renormalization rule

`score_unit(dim_scores, spec)` turns `{dimension_name: raw_score | None}`
into `{composite, band, renormalized, renormalization_note}`.

For each dimension with a score, the raw value is converted to a 0.0-1.0
fraction of that dimension's tiled range (`(value - dim_min) / (dim_max -
dim_min)`), then weighted and summed. The result is scaled to 0-100.

**Missing dimensions never skew the composite invisibly.** When a
dimension is `None` (failed research leg, no ledger history, no
coordinates), its weight is dropped and every *available* dimension's
weight is renormalized proportionally: `weight_i / sum(available
weights)`. The unit's `renormalized` field lists exactly which dimensions
were missing, and `renormalization_note` is a plain-English trace, e.g.
`"scored on 3 of 4 dimensions; weights renormalized"` - present whenever
at least one dimension is missing, absent when the unit scored on every
dimension. If every dimension is missing, `composite` is `None` and
`band` is the literal string `"n/a - unscored"` - never a defaulted band.

### Fractional scores and the permitted integer gap

`dim_scores` is typed `float | None` - a raw score is "a number" a
research subagent returns (or, for `move_in_window_fit`, the output of
`move_in_fit_score()`, below), and any of those can be non-integer even
though the committed anchor bands are written with integer bounds. Section
1 noted that `load_spec()` tolerates a gap of up to 1 between adjacent
integer bounds (e.g. one band ending `score_max: 16`, the next starting
`score_min: 17`); a fractional value such as `16.5` can land inside that
permitted gap with no band matching it exactly. `score_unit` never raises
on this: the value is snapped to the nearer band by distance to its
`[score_min, score_max]` interval, with an exact midpoint tie (only
possible when the gap is exactly 1) resolved to the higher, more-favorable
band. This is a documented, deterministic rule about which *band* a value
is treated as belonging to for `rank_based` scoring - it never alters the
score value itself, and non-`rank_based` dimensions do not need a band
match at all (their fraction is a direct linear function of the clamped
numeric value against the dimension's full range, independent of which
band it falls in).

### `rank_based` dimensions

A dimension marked `rank_based: true` converts its raw score into the
**ordinal position of the matched anchor tier** (0.0 for the worst tier,
1.0 for the best, evenly spaced by tier count) instead of a linear
fraction of the numeric range. This is the mechanism the ticket calls
"rank-based scoring so one extreme value cannot dominate a thin
composite": a dimension whose raw numbers can spike wildly (e.g. a
leverage or distress-derived score) only ever contributes based on which
tier it landed in, not how far past the tier boundary the raw number went.
None of the v1 dimensions (commute, budget_fit, safety, cleanliness,
move_in_window_fit) need this - it is there for future outlier-prone
dimensions, added the same way any new dimension is added: a new block
under `dimensions:` in `scoring.yml`, no code change.

## 3. Rarity calibration

`action_bands` in the spec sets `tour_now_min` and `watch_min` on the 0-100
composite scale. `calibrate(bands, spec)` takes a flat list of band
strings (one per live, already-gated unit - see below) and reports
`{share_tour_now, ok, warning, sample_size}`. The spec's
`calibration.tour_now_max_share` (default in the example: `0.15`) is the
target: TourNow should stay rare. When the actual share exceeds it,
`warning` names the overage and recommends raising
`action_bands.tour_now_min` in `scoring.yml`. **Threshold changes are a
human commit to the spec file, never something `calibrate()` or any other
function does automatically** - `calibrate()` only measures and reports.

## 4. The TourNow gate interaction

`band_with_gates(band, tour_now_blocked)` is the seam between this module
and the tri-state hard-gate evaluator (see the scan epic's gate work): if
any hard gate on a unit is `UNKNOWN`, the caller passes
`tour_now_blocked=True` and a `"TourNow"` band is demoted to `"Watch"`.
`"Watch"` and `"Skip"` pass through unchanged. This keeps the absolute
rule intact: no unit reaches TourNow while any gate result is unresolved,
regardless of how strong its composite score is. Feed `calibrate()` the
**post-gate** bands (after `band_with_gates` has been applied to every
unit) so the TourNow share it reports matches what actually renders on the
dashboard.

## 5. Market-distress flags

`distress_flags(events, spec)` reads `spec["distress"]["severity"]` (a
committed table of `{flag, points, meaning}`) and `spec["distress"]
["stale_factor"]` (default `1.5` if unset) and evaluates three flags from
ledger-derived `events`:

- `relisted` - `events["relist_count"] >= 1`
- `price_cuts_30d` - `events["cuts_30d"] >= 2`
- `stale` - `events["days_listed"] > events["neighborhood_median_days"] *
  stale_factor`

Any `events` field that is `None` simply cannot trigger its flag - it does
not raise, and it does not get treated as zero. A unit with no ledger
history at all will correctly get an empty flag list from this function;
**it is the caller's job**, reading the snapshot ledger directly, to
render "no history yet" for such a unit rather than presenting an empty
flag list as "no distress observed" (those are different claims, and only
the caller - who can see whether history exists - can tell them apart).

Flag names and their `meaning` text are deliberately framed as
negotiation-positive leverage signals for the renter, never as
scam/fraud language. Listing-legitimacy checks are a separate system.

## 6. Concession-climate sample-size disclosure

`concession_climate(stats, spec)` classifies a building or neighborhood as
`landlord-favorable`, `neutral`, or `renter-favorable` from `stats =
{tracked, with_cuts, median_days_live, concession_mentions}`.

Below `spec["climate"]["min_sample"]` (default `5`), the function returns
`{"classification": None, "reason": "sample below minimum", "sample_size":
tracked, "min_sample": N, "disclosed": True}` - render this as
`"insufficient sample (n=X)"`, never as a low-confidence classification.

At or above the minimum, `sample_size` is always present alongside the
classification (`"disclosed": True` on every branch) so a chip can never
show a climate label without its n. Classification compares `cut_share =
with_cuts / tracked` and `concession_rate = concession_mentions / tracked`
against four thresholds in `spec["climate"]` (`cut_share_renter_favorable`,
`concession_rate_renter_favorable`, `cut_share_landlord_favorable`,
`concession_rate_landlord_favorable`) - documented defaults live in
`scoring.py` (`DEFAULT_CLIMATE_THRESHOLDS`) and only need to be listed in
`scoring.yml` to override them.

This module does not fetch the FRED regional-CPI backdrop mentioned in the
epic - that is a separate, explicitly keyless-or-n/a probe belonging to
the research stage. `concession_climate()` classifies from ledger signals
only; a caller wiring in a CPI backdrop should attach it alongside this
function's output, flagged `ledger-only` when the backdrop fetch failed,
exactly as the epic describes.

## 7. Negotiation anchor usage

`negotiation_ask(unit, climate, comps)` computes one concrete ask:
weeks free, or the equivalent dollars-per-month off. **This is a number
the user says in person or types into an application themselves.**
Neither this function nor anything that calls it emails, DMs, or
form-fills an offer to a landlord or broker.

Inputs:
- `unit`: `{"cuts": int|None, "days_listed": int|None, "current_price":
  number|None}`
- `climate`: a `concession_climate()` result (needs `classification` and
  `sample_size`)
- `comps`: optional `[{"note": str, "price": number}]` - only a comp
  strictly cheaper than `unit["current_price"]` is cited

If any of `cuts`, `days_listed`, `current_price` is `None`, or `climate`
has no real classification (insufficient sample), the function returns
`{"ask": None, "justification": [], "insufficient_data": True, "reason":
"insufficient data - no anchor computed", "missing": [...]}` naming what
is missing - never a guessed number from thin evidence.

Formula, reproducible from the inputs alone:

```
weeks_free  = 1.0 * cuts                                  # each observed cut
            + (1.0 if days_listed >= 30 else 0)
            + (1.0 if days_listed >= 60 else 0)            # up to +2 total
            + (1.0 if climate renter-favorable else
               -0.5 if climate landlord-favorable else 0)
weeks_free  = max(0.0, weeks_free)

monthly_off = weeks_free * current_price / 52
```

`monthly_off` is the dollar value of `weeks_free` of free rent, amortized
evenly across a 12-month lease and expressed as a flat monthly discount
(`weeks_free` weeks of rent at the weekly-equivalent of `current_price`,
divided across 12 months). Every string in `justification` is built only
from the values passed in (cut count, days listed, climate classification
and its `n`, and - if present - the cheapest qualifying comp's price and
note); nothing is invented or pulled from outside the function's
arguments.

A static seasonality note (leasing-season strength by month) belongs in
the tour-dossier renderer that calls this function, not in
`negotiation_ask()` itself - `negotiation_ask()` only computes the
evidence-linked number.

## 8. Move-in-window fit

`move_in_fit_score(available_date, window_start, window_end, spec)`
computes the raw score for the `move_in_window_fit` dimension (weight and
anchor bands committed in `scoring.yml`, alongside every other dimension -
see section 1) from the gap between a unit's available date and the
caller-supplied move-in window. This function does not read `config.yml`
itself; the caller reads the move-in window from `config.yml` and passes
`window_start`/`window_end` in, the same pattern `distress_flags()` and
`concession_climate()` use for ledger-derived and stats inputs.

Inputs `available_date`, `window_start`, `window_end` each accept an ISO
`"YYYY-MM-DD"` date string or a `datetime.date`. `gap_days` is `0` when
`available_date` falls inside `[window_start, window_end]`; otherwise it
is the number of days to the nearer window edge. `gap_days` is bucketed
against three thresholds in `spec["move_in_fit"]`
(`perfect_max_days`, `minor_max_days`, `moderate_max_days` - committed
defaults `0` / `14` / `42` in the example spec, code fallbacks in
`scoring.py`'s `DEFAULT_MOVE_IN_*` constants), and the score returned is
the top of that tier's anchor band - `20.0`, `16.0`, `12.0`, or `8.0` -
so the result always lands cleanly inside the `move_in_window_fit` band
whose `meaning` text matches, with no invented in-between precision.

Returns `None` ("n/a") when `available_date` is missing (the listing
states no availability), when `window_start` or `window_end` is missing
(no move-in window configured), or when any of the three fails to parse
as a date - never a guessed midpoint, and never a crash on a messy scraped
date string. A `None` return is treated by `score_unit` exactly like any
other missing dimension: its weight is proportionally redistributed
across the unit's other available dimensions (section 2), and the
renormalization note records that `move_in_window_fit` was one of the
dimensions the unit was not scored on.
