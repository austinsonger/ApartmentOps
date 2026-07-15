# ApartmentOps file contracts

Every stage communicates through these files, all under `apartmentops/` in
the project root. A stage must read its inputs from here and write its
outputs here - nothing else is shared state. Timestamps are ISO 8601 with
timezone. Prices are monthly USD numbers (no strings, no "$").

**unit_id convention (used by every per-unit file below):** slugified
building name + "-" + unit token, e.g. `example-tower-2-3410`. This is what
`backlog.unit_key()` in `scripts/backlog.py` produces; use it everywhere a
file keys on a unit so joins stay trivial.

## apartmentops/config.yml  (written by: onboard; read by: everything)

```yaml
version: 1
created: 2026-01-01T09:00:00-05:00
anchor:                      # null if the user skipped commute ranking
  label: "Office"
  address: "verified street address"
  lat: 40.0
  lon: -74.0
budget:
  gross_min: 4000
  gross_max: 5000
  target: 4500
  allow_net_effective: true  # net-in-band qualifies if gross <= gross_max_stretch
  gross_max_stretch: 5500
  income_annual: null        # optional; enables 40x qualification flags
unit:
  beds: 2
  baths: {value: 2, mode: hard}      # mode: hard | bonus
  sqft_min: {value: 1000, mode: bonus}
gates:                       # each: {value, mode: hard|bonus}
  year_built_min: {value: 2019, mode: bonus}
  floor_min: {value: 10, mode: bonus}
  exposure_preferred: [S, SE, SW]    # empty list = no preference
  exposure_blocked: [N]              # hard-blocked directions
  elevator_doorman: {value: true, mode: bonus}
  in_unit_laundry: {value: true, mode: bonus}
  pets: none
geography:
  areas: ["area-slug-1", "area-slug-2"]
  notes: "free text on scope decisions"
move_in_target: null         # optional ISO date; drives the dashboard countdown
saved_searches:              # optional; built by onboarding from the gates above
  primary:                   # profile name; extra profiles (e.g. a 1BR fallback)
    platform-a:              # write to the same verified.json with a profile tag
      area-slug-1:
        url: "https://... newest-first list URL with all filters baked in"
        marker: "of \\d+ results"   # optional regex proving results rendered
```

`income_annual` lives ONLY in this user-owned file. It must never be copied
into any generated artifact (`costs.py` takes it as a separate argument and
refuses inputs that embed it).

Onboarding also copies `examples/scoring.example.yml` to
`apartmentops/scoring.yml` (the committed scoring spec the research stage
scores against - see `references/scoring.md`) and creates
`apartmentops/data/actions.yml` (see below).

## apartmentops/data/candidates.json  (written by: scan, hunt phase)

Raw finds before verification. Array of unit objects:

```json
{
  "building": "Example Tower 2",
  "address": "1 Example Ave, City, ST",
  "area": "area-slug-1",
  "unit": "3410",
  "floor": 34,
  "rent_gross": 4810,
  "rent_net": null,
  "concession": "up to 1 month free on select units",
  "beds": 2, "baths": 2, "sqft": 1048,
  "available": "2026-08-13",
  "year_built": 2021,
  "light_view": "SW corner per official key plan",
  "url": "https://... (page the fact came from)",
  "verify_url": "https://... (best headless-loadable page)",
  "source": "building-direct",
  "found_at": "2026-01-01T18:00:00-05:00"
}
```

## apartmentops/data/verified.json  (written by: scan, verify phase; read by: research, dashboard)

Same objects as candidates, plus verification fields. Only rows that were
actually checked belong here - live or not. On disk this is an ARRAY of
unit objects (like candidates.json); scripts that need a dict key it by
`unit_id` via `backlog.unit_key()` (`backlog.load_verified_units` accepts
either shape and normalizes).

```json
{
  "live": true,
  "verified_at": "2026-01-01T18:24:00-05:00",
  "rent_verified": 4810,
  "rent_is_net": false,
  "unit_deep_link": "https://... (per-unit page if one exists, else null)",
  "screenshot": "apartmentops/shots/tower2-3410.png",
  "scam_flags": [],
  "profile": "primary",
  "extraction": {"rent_verified": "embedded:next_data", "beds": "dom"},
  "gates": {"baths": "PASS", "year_built_min": "UNKNOWN"},
  "verify_checklist": ["confirm year built before contacting or touring"]
}
```

Field-level provenance (additive, preferred for new writes): any fact field
MAY be a provenance object instead of a bare scalar -
`{"value", "status": "FACT|INFERRED|MISSING|CONFLICT", "source", "evidence",
"confidence"}`. Legacy bare scalars are read as FACT with no source. Rules,
helpers (`gates.normalize_field`, `gates.field_value`), the tri-state gate
evaluation, and the machine-readable anti-fabrication constraints that every
scan subagent prompt must carry verbatim are in `references/provenance.md`
and `references/constraints.json`. `scam_flags` is populated from the
photo-hash net (`scripts/photo_hash.py`, see `data/photo_hashes.json` below)
as well as the text scam screen.

Re-verification (hydrate) updates `live`, `verified_at`, `rent_verified` in
place and never deletes rows - a gone unit with `live: false` is market
history worth keeping. Every hydrate also appends to the snapshot ledger and
run log (next section) - that is what powers deltas, sparklines, and the
freshness banner.

## apartmentops/data/snapshots.jsonl and data/run-log.jsonl  (written by: every hydrate; read by: research, dashboard)

Append-only market memory. One `snapshots.jsonl` row per unit per hydrate:
`{unit_id, url, observed_at, run_id, price, availability, status:
"live"|"gone"|"recheck", fetch_evidence}`. One `run-log.jsonl` row per
hydrate ALWAYS - zero-change runs included - so a missing weekly row means
the scheduler died (the dashboard banner turns red via
`snapshots.heartbeat_overdue`). Full schemas, the run_id rules, and the
loud-error contract (`diff_last_two` raises on fewer than two runs - an
empty diff is never fabricated) are in `references/ledger.md`;
`scripts/snapshots.py` is the only writer.

## apartmentops/data/actions.yml and data/backlog-state.json  (actions: USER-OWNED)

`actions.yml` tracks what the USER has done about each unit - orthogonal to
listing liveness: `{unit_id: {status: NEW|ToContact|Contacted|TourBooked|
Toured|Applied|Rejected|Skipped|Secured, note, updated_at}}`. The pipeline
may ONLY append NEW entries for newly verified units
(`backlog.append_new_entries`, which preserves the user's comments and
ordering); every other edit is the user's. Resurfacing decay counters are
pipeline state and live separately in `backlog-state.json`. Details and the
resurface-max-3-then-stale rule: `references/actions.md`.

## apartmentops/data/buildings.json  (written by: research; read by: dashboard, costs)

Per-building enrichments keyed by building slug:

```json
{
  "example-tower-2": {
    "address": "1 Example Ave, City, ST",
    "lat": 40.0, "lon": -74.0,
    "flood": {"zone": "X", "sfha": false, "source_url": "https://...", "viewer_url": "https://...", "fetched_at": "..."},
    "rent_control": {"status": "UNKNOWN", "cap_pct": null, "source": null, "resolved_at": null}
  }
}
```

`flood` comes from `scripts/flood.py` (keyless FEMA NFHL query; `zone: null`
plus an `error` field on service failure - render n/a, never guess).
`rent_control` follows the cite-or-UNKNOWN procedure in
`references/costs.md` and caps the year-2 renewal assumption in
`scripts/costs.py`.

## apartmentops/data/scores.json  (written by: research; read by: dashboard, backlog)

```json
{
  "example-tower-2-3410": {
    "composite": 78.5,
    "band": "Watch",
    "renormalized": [],
    "renormalization_note": null,
    "dims": {"commute": 18, "budget_fit": 15, "safety": 14, "cleanliness": 12, "move_in_window_fit": 17},
    "distress": [{"flag": "cuts_30d", "severity": "moderate", "meaning": "2 price cuts in 30 days"}]
  }
}
```

Produced by `scripts/scoring.py` against `apartmentops/scoring.yml` (verbal
anchor bands constrain subagent numbers; missing dimensions renormalize with
a recorded note; TourNow demotes to Watch while any hard gate is UNKNOWN;
TourNow must stay rare - `scoring.calibrate` warns past the spec share).

## apartmentops/data/lease.json  (written by: apartmentops-lease; read by: dashboard)

`{unit_id, extracted_at, fields: {provenance objects with page/clause
cites}, deviations: [{field, advertised, lease, citations}], dates:
[{date, label, computed_from, status: "upcoming"|"past_due"}], skipped:
[{label, missing}]}`. Field dictionary: `references/lease-fields.md`; date
math: `scripts/lease_dates.py`. Calendar entries are offered as text for the
user to add themselves - never auto-added.

## apartmentops/data/photo_hashes.json  (written by: scan; read by: scan, dashboard)

Persisted output of `photo_hash.build_index()` over archived listing photos
and screenshots: `{generated_at, photos: [{unit_id, address, platform,
price, live, photo_path, ahash, dhash, error}]}`. `photo_hash.find_matches`
flags cross-address / cross-platform / price-gap / zombie-repost pairs;
matches map onto the flagged units' `scam_flags` with the evidence pair. A
unit absent from `photo_hash.photo_coverage()` renders "photo check: n/a" -
never a default clean state.

## apartmentops/data/lines/{building-slug}.yml  (written by: research; read by: research, dashboard)

Per-line orientation maps from cited official floor plans / key plans, for
the intra-building line-substitution advisor (`scripts/line_advisor.py`).
Schema and the trailing-letter trust rule: `references/line-substitution.md`.
No line map for a building means no suggestions - silence, not inference.
The research stage's derived index lands at `apartmentops/data/lines.json`
(`{"lines": index, "report": report}` - exactly what the line_advisor CLI
emits; the report's `no_line_map` / `unparsed_unit` counts make exclusions
visible without inventing data).

## apartmentops/data/results.json  (written by: dashboard hydrate via scripts/costs.py; read by: dashboard)

`costs.py inputs.json > results.json` - the only source of money math on
the dashboard (true monthly cost, 12/24-month TCO, waterfall, renewal
scenarios), with the declared `assumptions` block embedded so every number
is reproducible. The transient `inputs.json` is assembled from config.yml,
verified.json, and transit.json; it must NEVER contain `income_annual`
(`costs.py` refuses it - income passes as a separate argument and the
qualification result never echoes a ratio it could be reconstructed from).

## apartmentops/extractors/{platform}.yml  (written by: scan bring-up probe; read by: verify pass)

Per-platform embedded-payload extractor specs recorded from one-time
empirical probes (`script#__NEXT_DATA__`, JSON-LD, window-state blobs).
Shape: see `examples/extractor.example.yml`. The verify pass reads fields
through `scripts/extract_embedded.py` first and falls back to DOM parsing,
tagging each field's `extraction` method in verified.json. Re-probe a
platform when its extractions start returning MISSING.

## apartmentops/data/areas.json  (written by: research; read by: dashboard)

One entry per `area` key that appears in verified.json:

```json
{
  "area-slug-1": {
    "safety": {"grade": "B", "summary": "one line of why, with numbers", "sources": ["url1", "url2"]},
    "cleanliness": {"grade": "B-", "summary": "one line of why", "sources": ["url1"]},
    "essentials": [{"name": "Grocery name", "type": "grocery", "walk_min": 3}],
    "vibe": "one or two honest sentences including risks"
  }
}
```

Grades are letters A-F with plus/minus, always paired with at least one
source URL. If an area was not researched, OMIT it (the dashboard renders
"n/a" chips) - never invent a grade.

Optional per-area `climate` field (from `scoring.concession_climate` over
the snapshot ledger): `{"classification": "landlord-favorable"|"neutral"|
"renter-favorable", "sample_size": 12}`. The sample size is ALWAYS disclosed
on the chip; below the spec minimum the classification is null with a
reason, and the chip renders n/a.

## apartmentops/data/transit.json  (written by: research; read by: dashboard)

```json
{
  "fares": {"note": "verified fare table with source urls", "sources": ["url1"]},
  "areas": {
    "area-slug-1": {
      "door_to_door_typical_min": 35,
      "range": "30-42",
      "route": "Walk 3 min to X, line Y to Z, walk 7 min",
      "monthly_cost": 130.20
    }
  }
}
```

Compute door-to-door as walk + typical peak wait + ride + final walk, using
real station walk times (marketing says 5 minutes; the map often says 12).

## apartmentops/dashboard.html  (written by: dashboard)

Self-contained HTML (no external hosts). Study
`assets/example-dashboard.html` for the working pattern: to-scale SVG map
from real shoreline polygons, walk/transit legs per building, grade chips
that LINK to their sources, per-unit rows with deep links and live/gone
badges, light and dark themes. Publish as an artifact and keep republishing
the same file path so the URL stays stable.

Beyond the map and cards, the dashboard renders (each degrades to n/a or
hides when its input file is missing - nothing is ever faked): a freshness
and confidence banner (last hydrate, research legs, green/yellow/red via the
run-log heartbeat), a weekly delta digest section (`snapshots.digest_markdown`),
per-unit price sparklines with NEW / PRICE-DROP / REMOVED badges (hidden
below two runs), an action panel grouped by `actions.yml` status with a
move-in countdown from `config.move_in_target`, a 2-8 unit compare pivot,
flood chips linking to the FEMA viewer, an action-band column
(TourNow / Watch / Skip), provenance chips on unit fields, a lease
critical-dates timeline when `data/lease.json` exists, and a printable
per-unit tour one-pager via print CSS (missing fields print as MISSING).

## apartmentops/shots/

Screenshot evidence from every verification pass. Never claim a unit was
verified without one.
