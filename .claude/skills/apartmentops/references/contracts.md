# ApartmentOps file contracts

Every stage communicates through these files, all under `apartmentops/` in
the project root. A stage must read its inputs from here and write its
outputs here - nothing else is shared state. Timestamps are ISO 8601 with
timezone. Prices are monthly numbers in the config's `locale.currency`
(USD when no `locale` block is set) - no strings, no currency symbols.

**unit_id convention (used by every per-unit file below):** slugified
building name + "-" + unit token, e.g. `example-tower-2-2207`. This is what
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
  broker_fee: {mode: bonus}  # hard = skip broker/agency ads; bonus = a negative only
unit:
  beds: 2
  baths: {value: 2, mode: hard}      # mode: hard | bonus
  sqft_min: {value: 1000, mode: bonus}
  whole_unit_only: true      # rooms in shared units and sublets are skipped
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
list_purpose: compare        # call_first (speed: freshest first, call-now framing)
                             # | compare (true cost, leverage, calm evaluation)
locale:                      # optional; omitted = US defaults shown here
  currency: USD
  timezone: America/New_York # reporting timezone; also the fallback for
                             # naive source timestamps ONLY when known correct
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

Criteria the user has not confirmed stay unset - never filled from these
examples or from a previous user's values. Onboarding shows the resulting
one-line search summary and gets a yes before the first scan.

## apartmentops/data/checked.json  (written by: scan; read by: scan, hydrate - private, never published)

The hunt's memory, maintained only through `scripts/checked.py`:
`rejected` (opened and dismissed: `{token: {source, reason, checked_at,
criteria, criteria_dependent}}`, where `criteria` is
`checked.criteria_fingerprint(config)`), `deferred` (filtered before
opening, by group: the first pool to revisit when the search widens),
`out_of_window`, `scan_state` (per run: pages expected vs scanned per
query, blockers, finish time) and `price_refresh` (per run: verified,
missing, unverified, changes). `checked.coverage(state, run_id)` is the
only way a run report may call a scan complete. Drained candidate rows
land in `apartmentops/data/drain/<run_id>.jsonl` as they arrive
(`checked.drain_append`). Mechanics: `references/collection-playbook.md`.

## apartmentops/sources.yml  (written by: scan as it meets each operator, hand-maintained; read by: scan verify, dashboard hydrate)

The per-source evidence policy: what each listing surface can prove.
Verdicts are per source, never per link type - an index page confirms live but never proves gone, a complete unpaginated operator table that omits a unit is gone evidence, and some operators render any invented unit id with a price on their per-unit deep link, so their index, not the deep link, is authoritative.
Matching is by substring against the URL and the FIRST match wins, so a per-unit path must be listed before the index pattern it sits under.
A URL with no matching entry falls back to `verify_units.py`'s heuristics (a URL naming the unit token is its own page, an index-looking path is an index), which cannot produce a false gone but will read an untrusted deep link as a false live - write the entry.

```yaml
sources:
  - match: "/inventory/unit/"   # substring of the URL this entry governs
    kind: untrusted                  # own_page | table | index | plan_page | aggregator | gated | untrusted
    live_evidence: false             # token present here proves live
    gone_evidence: false             # token absent here proves gone (own_page and complete tables only)
    price_evidence: false            # a price read here may be written
    note: "renders any invented unit id with a price; the index below is authoritative"
  - match: "/inventory"
    kind: table
    complete: true                   # table only: unpaginated, lists every unit
    live_evidence: true
    gone_evidence: true
    price_evidence: true
    price_layer: base                # net | base | total when the page itself does not say
```

Starter file with every kind: `examples/sources.example.yml`.
Prices are admissible only from the unit's own page (`own_page`) or the unit's own row in a `table`; a price near a unit token on an index is never written, and the detected layer (a net-effective asterisk, "base rent", "total monthly") travels with the price so a net figure is never stored as gross.

## apartmentops/data/candidates.json  (written by: scan, hunt phase)

Raw finds before verification. Array of unit objects:

```json
{
  "building": "Example Tower 2",
  "address": "1 Example Ave, City, ST",
  "area": "area-slug-1",
  "unit": "2207",
  "floor": 22,
  "rent_gross": 4380,
  "rent_net": null,
  "concession": "up to 1 month free on select units",
  "beds": 2, "baths": 2, "sqft": 1130,
  "available": "2026-09-02",
  "year_built": 2019,
  "light_view": "south-facing per key plan",
  "url": "https://... (page the fact came from)",
  "verify_url": "https://... (best headless-loadable page)",
  "source": "building-direct",
  "found_at": "2026-01-01T18:00:00-05:00"
}
```

`light_view` is the raw orientation text exactly as the listing or key plan states it.
It is input only: the verify phase normalizes it into the canonical `exp` object (next section) and nothing downstream reads `light_view`.

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
  "rent_verified": 4380,
  "rent_is_net": false,
  "unit_deep_link": "https://... (per-unit page if one exists, else null)",
  "screenshot": "apartmentops/shots/tower2-2207.png",
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

### Listing hygiene fields (optional, additive)

Written by the scan stage when the source provides them
(`references/collection-playbook.md`):

- `token`, `source` - the platform's own listing id and platform name;
  the first dedupe key.
- `published_at` - the ORIGINAL publication timestamp, kept whole, with
  `publication_timezone` when the source gives local time. Never the
  refreshed `updated_at` (kept separately when present). Publication,
  observation (`found_at`), verification (`verified_at`), and removal
  (`delisted`) dates stay distinct.
- `advertiser_type` (`owner` | `building_direct` | `agency` | `broker`),
  `agency`, `broker_fee`.
- `rooms`, `size_sqm` for locales that count rooms or meters.
- `fees`: `[{type, amount, currency, billing_period: monthly|bimonthly|
  quarterly|semiannual|annual|one_time, source_url, observed_at}]`.
  `quality.known_monthly_total` returns a partial total plus the missing
  components whenever any fee lacks an amount or period.
- `availability_state`: `active` | `possibly_missing` | `unknown` |
  `delisted`, managed by `scripts/feed_refresh.py`; `delisted` is set only
  by a confirmed `gone` verdict and carries the date. `price_checked` is
  the date a price was last actually read.
- `price_history`: `[{amount, currency, observed_at, source_url}]`.
- `alternative_sources`, `duplicate_candidates` - from
  `scripts/dedupe.py`; a duplicate is merged only on a token or strong
  match and never loses its URL or price evidence.
- `quality_flags` - `quality.traps` output; any `no_star` flag keeps the
  unit out of TourNow and off value badges.
- `notes` - absolute dates only, true cost first when it breaks the
  ceiling; never relative time.

### Orientation: the canonical `exp` object

Orientation is spelled exactly one way on a verified unit: `exp`.
The scan phase's `light_view` string is raw input that the verify phase normalizes into it; `exposure` is not a unit field anywhere in the pipeline (config.yml's `exposure_preferred` / `exposure_blocked` are gate names, not fields).

```json
"exp": {"dir": "S", "south": true, "conf": "HIGH", "src": "Floor-plan key plan: south-facing glass across the living room"}
```

- `dir`: a compass string (`N`, `NE`, `E`, `SE`, `S`, `SW`, `W`, `NW`) or null when unknown.
- `south`: `true` (verified south-facing glass), `false` (verified not south), or `null` (unresolved).
- `conf`: `HIGH` / `MED` / `LOW` / `UNK` - the three-signal orientation consensus (floor-plan key plan, footprint bearing, listing statement) confidence.
- `src`: the evidence, a URL, file path, or verbatim quote; never empty when `conf` is not `UNK`.

The dashboard renders `exp` as the Exp badge (`south: true` sun badge, suspected-but-unverified southerly line amber, verified not-south plain, unknown muted).
An exposure hard gate never reads `exp` directly: the producer derives the gate's field with `gates.exp_gate_field(exp)` (HIGH -> FACT, MED / LOW -> INFERRED and therefore UNKNOWN at the gate, UNK or null `dir` -> MISSING) and evaluates against a copy of the unit whose `exp` is that provenance object - see `references/provenance.md`.
UNKNOWN is a valid, correct result; never assert south without evidence.

Re-verification (hydrate) updates `live`, `verified_at`, `rent_verified` in
place and never deletes rows - a gone unit with `live: false` is market
history worth keeping.
Each re-check yields a three-state verdict (`scripts/verify_units.py`, per the `sources.yml` policy above): `live` updates all three fields (`rent_verified` only when the price is admissible), `gone` sets `live: false` and `verified_at`, and `check` (a wall, shell, index without the token, fetch error, untrusted surface, or a live read contradicting a prior gone) touches nothing - the prior verdict and its date are carried, and a check can never overturn a prior gone.
Every hydrate also appends to the snapshot ledger and
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
    "rent_control": {"status": "UNKNOWN", "cap_pct": null, "source": null, "resolved_at": null},
    "grating": {"stars": 4.3, "count": 1152, "url": "https://www.google.com/maps/search/?api=1&query=..."},
    "parking": {"avail": "garage", "cost": 250, "note": "on-site garage; $250/mo per <source>", "url": "https://..."},
    "gate": {"ok": true, "why": "every hard gate PASS: baths 2 (listing), in-unit laundry (building FAQ)"},
    "fees": [{"label": "amenity fee", "monthly": 75, "source": "https://...", "mandatory": true}]
  }
}
```

`flood` comes from `scripts/flood.py` (keyless FEMA NFHL query; `zone: null`
plus an `error` field on service failure - render n/a, never guess).
`rent_control` follows the cite-or-UNKNOWN procedure in
`references/costs.md` and caps the year-2 renewal assumption in
`scripts/costs.py`.
`grating` is the building's Google Maps rating, name-verified against the
listing; `count` is null when the signed-out Maps view hides it; the whole
object is absent (chip renders n/a) when no rating was verified. `parking`
records on-site parking: `avail` is `garage`/`valet`/`none`/`unknown` and
`cost` is a monthly rate ONLY when a real source publishes it (else null,
with the availability still shown). Both feed the dashboard's chips and the
all-in cost figure.
`gate` is the building-level roll-up of the hard gates, tri-state: `ok: true` when every hard gate on every live unit is a FACT-backed PASS, `false` when any is a FAIL (the dashboard marks the building disqualified and sorts it last in every order), `null` when unresolved (rendered as a distinct unknown chip, never as a pass or a fail); `why` names the gates and sources behind the verdict.
`fees` lists known mandatory monthly fees with a published amount and a source; the dashboard's all-in figure adds them, an empty list adds nothing and reads "none on file", and a fee is never guessed.
A unit's window exposure lives on the unit in verified.json as the canonical `exp` object (see the verified.json section above).
A second
commute anchor is an optional `commute2` block (same shape as the
transit.json commute, `legs` ending at `office2`) for a two-office
household; the dashboard draws both and shows both per unit.

## apartmentops/data/scores.json  (written by: research; read by: dashboard, backlog)

```json
{
  "example-tower-2-2207": {
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

Beyond the map and cards, and not shown in the example file, the dashboard
renders (each degrades to n/a or hides when its input file is missing -
nothing is ever faked): a freshness
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
