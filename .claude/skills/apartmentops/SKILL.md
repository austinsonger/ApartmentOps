---
name: apartmentops
description: >-
  ApartmentOps - a verified apartment-hunting pipeline anyone can set up from
  scratch. Onboards a new user (commute anchor, budget band, must-haves), then
  routes to the companion skills that feed each other: apartmentops-scan (find
  + browser-verify units), apartmentops-research (safety, cleanliness, transit
  cost), apartmentops-dashboard (interactive map dashboard + re-hydration),
  and apartmentops-lease (post-signing lease review and critical dates).
  Use this skill whenever the user wants to find an apartment or rental,
  compare listings against a commute, set up ApartmentOps, run an apartment
  search, or asks anything like "find me a 2 bed near my office", "is this
  neighborhood safe", or "update my apartment dashboard" - even if they never
  say the word ApartmentOps.
---

# ApartmentOps

A pipeline that turns "find me an apartment" into a verified, mapped,
evidence-backed shortlist. Six skills feed each other through files in the
`apartmentops/` directory of the project (the contracts are defined in
`references/contracts.md` - read it before producing or consuming any stage
file):

```
onboard (this skill)  ->  apartmentops/config.yml
apartmentops-scan     ->  apartmentops/data/verified.json  (+ shots/)
apartmentops-research ->  apartmentops/data/areas.json, transit.json
apartmentops-dashboard->  apartmentops/dashboard.html  (published artifact)
apartmentops-lease    ->  apartmentops/data/lease.json  (post-signing, optional)
apartmentops-sync     ->  the user's shortlist document  (optional)
```

## Routing

Check state, then route. Run these checks silently first:

1. `apartmentops/config.yml` exists? If NOT -> run Onboarding below.
2. Config exists but no `data/verified.json` -> the user needs a scan; invoke
   the `apartmentops-scan` skill.
3. Verified data exists but no `data/areas.json` / `data/transit.json` ->
   invoke `apartmentops-research`.
4. All data present -> `apartmentops-dashboard` builds or re-hydrates.
5. The user has a draft lease PDF (in `apartmentops/data/leases/`) or asks a
   lease question - "review my lease", "check this against the listing",
   "when do I need to give notice" - invoke `apartmentops-lease`. This one is
   orthogonal to the checks above: it runs whenever the user has a lease in
   hand, regardless of where the rest of the pipeline is (it degrades
   gracefully with no `verified.json` to compare against).
6. After the dashboard: if `shortlist_sync.enabled` and new verified units
   exist or the sync cadence (`min_days_between_syncs`) has passed, offer
   `apartmentops-sync` to update the user's shortlist document.

The stages are separable on purpose: a user can re-run scan weekly without
re-onboarding, or re-hydrate the dashboard without re-scanning. Never skip a
missing upstream contract silently - tell the user which stage is missing and
offer to run it.

## Onboarding (from scratch)

The goal is a complete `apartmentops/config.yml` in one conversation. Ask for
what only the user knows; compute what can be computed. Do not fabricate any
value - every field below is either user-supplied or derived from a source you
actually fetched.

### Step 1 - The commute anchor (most important question)

Ask where they commute to, down to the street corner. The anchor changes
everything: a downtown office and a midtown office produce completely
different winning neighborhoods, and marketing walk times lie.
Verify the address exists (web search), then geocode it with
`scripts/geocode.py` (Nominatim, 1 req/sec). Store address + lat/lon.

If they work from home, use their most frequent destination or skip
commute ranking (set `anchor: null`) - the pipeline still works.

### Step 2 - Budget, honestly

Ask for the monthly budget as a range, then explain the one trap that
matters: advertised "net effective" prices bake in a free-month concession
and revert to the higher gross at renewal. The config stores BOTH a gross
band and whether net-effective prices may qualify. If they share income,
note the standard qualification rule (annual gross >= 40x monthly rent) so
every future result can carry a "you qualify" flag - but never require it.

Ask about broker fees: a deal-breaker (`budget.broker_fee.mode: hard`,
so broker and agency ads are skipped) or just a negative (`bonus`)? Where
the same unit is offered directly and through a broker, the pipeline keeps
the direct ad either way.

Ask for a deliberate rent floor (`gross_min`), not just a ceiling: far
below the local market, a "whole apartment" is usually a room in a shared
unit or bait, and the floor is what removes them. Record
`unit.whole_unit_only` (default true) so shared rooms and sublets are
skipped.

Also ask for a target move-in date - optional, `null` is fine if they are
just browsing. Store it as the top-level `move_in_target` (ISO date) in
`config.yml`; it feeds scoring's `move_in_window_fit` dimension (how well a
unit's availability lines up with when they actually need to move) and
drives the dashboard's move-in countdown.

### Step 3 - The unit itself

Beds, baths (ask whether 2 full baths is a requirement or a preference -
this cuts pools in half), minimum square feet if any.

### Step 4 - Quality gates and deal-breakers

Ask which of these are hard requirements vs scoring bonuses, and record each
as `hard` or `bonus`: building age (e.g. built in the last N years), minimum
floor, elevator/doorman, light and exposure (e.g. south-facing; record
hard-blocked directions too - a "no north" rule is common), pets, parking,
in-unit laundry. Lesson learned: when everything is a hard gate the pool goes
to near zero; when everything is a bonus the results feel old and dark. Make
the user choose consciously.

Then ask for deal-breaker words and must-have words, matched against each
feed card's title and description: for example `exclude_keywords:
["basement", "garden unit"]` and `include_keywords: ["in-unit laundry"]`.
Store them under `filters:`. They match whole words and phrases only, and
they are criteria: changing them later re-opens every listing previously
rejected for a keyword.

### Step 5 - Geography and locale

Which neighborhoods/boroughs/cities are in scope. Offer to widen: the best
value is often one transit stop past where the user first looked.

Outside the US, set the `locale` block (currency and reporting
timezone) and never silently change country or currency. Read platform
area codes from a real search URL, never guess them.

### Step 5b - What the list is for

Ask whether the user will work the list by calling first (speed: freshest
listings first, call-now framing for a hot find) or by comparing calmly
(true monthly cost, negotiation leverage). Store it as `list_purpose:
call_first | compare`; it shapes the notes and the run report, not the
filters.

### Step 6 - Write the config and confirm

Write `apartmentops/config.yml` exactly per `references/contracts.md`,
create `apartmentops/data/` and `apartmentops/shots/`, echo a one-line
search summary back (areas, band with floor, beds/rooms, hard gates,
broker-fee rule) and get a yes before the first scan
(`apartmentops-scan`). Never use the example configs as the user's
preferences; anything unconfirmed stays unset.

When the user later says results feel off - wrong area, wrong band, too
many broker ads - it is nearly always a config problem, not a scraping
problem: re-ask the relevant question briefly and update `config.yml`.
Criteria-dependent rejections in `checked.json` re-open automatically when
the criteria change.

Step 6 also:

- Copy `examples/scoring.example.yml` to `apartmentops/scoring.yml` - this
  is the committed scoring spec the research stage scores against
  (`references/scoring.md`). Mention to the user that they can retune the
  dimension weights or verbal anchor bands later; it is their file once
  copied, not something later stages overwrite.
- Create `apartmentops/data/actions.yml` empty, with a one-line comment
  header saying it is user-owned (the pipeline only ever appends `NEW`
  entries for newly verified units; every other edit is the user's - see
  `references/actions.md`).
- Build the `saved_searches` block from the gates just chosen: read
  `references/platforms.md`, ask which platforms to enable (default: all
  six for a US city), collect `locale.platform_slugs` (`city_slug`,
  `craigslist_subdomain`, `area_slugs`), call
  `doctor_searches.build_saved_searches(config, recipes)` with the
  templates from that file, run `check_saved_searches` on the result, and
  show the preflight table (a bot wall is a reported result, not a
  failure). For Redfin, drive the UI once per area and store the URL it
  produces in place of the `needs_ui` leaf. Ask for `locale.country` when
  it is unset; a non-US country skips the US-only recipes. The result is
  keyed `saved_searches.<profile>.<platform>.<area>: {url,
  marker}` per `references/contracts.md`. `marker` is an optional regex
  (e.g. `"of \\d+ results"`) proving the page actually rendered results,
  not an empty or blocked state.
- Explain the optional secondary profile: a second, differently-gated
  search (e.g. a 1BR fallback if the 2BR pool runs dry) can be added as its
  own key under `saved_searches` alongside `primary`. It writes to the same
  `apartmentops/data/verified.json`, tagged with its own `profile` value,
  so the dashboard can filter or label results by which search found them.

## Ground rules for every stage (repeat these to any subagent you spawn)

- **Anti-fabrication:** report only facts tied to a URL actually fetched or a
  file actually read. UNKNOWN is always an acceptable value. Never invent
  unit numbers, rents, years, orientations, or safety claims.
- **Constraints travel verbatim, and outputs get graded:** every subagent
  prompt carries `references/constraints.json`'s rules verbatim, not
  paraphrased or summarized. After a stage produces output, grade it with
  `scripts/gates.py`'s `grade_fields` - autofail on `value-without-provenance`
  (a FACT/INFERRED value with no source) and `placeholder-left-in-output`
  (TODO/TBD/FIXME/lorem-ipsum/example.com left in a committed file). A unit
  that fails grading is withheld and re-checked next run, never shipped with
  a footnote.
- **Liveness or it does not count:** a listing must be verifiably live on the
  day of the check. Stale syndication pages ("zombie listings") look real and
  are not - years-old listings resurface on syndication pages looking current.
- **Evidence is per source, not per link type:** an index or root page confirms live but never proves gone; a login wall, bot wall, CAPTCHA interstitial, empty shell, or fetch error keeps the prior verdict and can never overturn a prior gone; a complete, unpaginated operator table that omits a unit IS evidence of gone; a per-unit deep link is admissible only when the source policy says so (some operators render any invented unit id with a price).
  Prices come only from the unit's own page or its own table row, with the price layer recorded; a price near a unit token on an index is unusable.
  The policy is the `sources:` list in `apartmentops/sources.yml` (`references/contracts.md`); harvest deep links when found, and record a policy entry for every operator you meet.
- **Human-in-the-loop:** never submit applications, book tours, send
  emails/messages to brokers or leasing offices, or click
  Book/Submit/Confirm/Apply on any site. Produce drafts and links; the user
  sends. Read-only browsing of public pages only; no logins, no CAPTCHA or
  bot-wall bypasses; back off on 403s and find another public source.
- **Absence is not removal:** a unit missing from a search feed is
  `possibly_missing` (or `unknown` on a partial scan), never gone, until
  its own surface confirms it. Never call a scan complete without
  `checked.coverage` saying so.
- **Absolute dates only:** notes never say "today", "yesterday", or
  "hurry"; they carry the date. Publication, observation, verification
  and removal dates stay distinct, and no timezone is ever invented.
- **Private stays private:** `checked.json`, drain files, contact details
  and working notes never go into a published dashboard.
- **Lessons go to the user first:** at the end of a round, report any new
  reusable mechanism learned (a trap, a broken URL form, a data pattern),
  without personal data. Edit a skill or reference file only when the
  user authorizes it.
- **No emojis in any produced file.**

## Bundled resources

- `references/contracts.md` - the file formats every stage reads/writes.
  Read it before any stage work.
- `scripts/geocode.py` - Nominatim geocoder (polite rate limit built in).
- `scripts/verify_units.py` - headless-Chromium liveness checker: feed it a
  JSON list of {key, token, url} plus `--policy apartmentops/sources.yml`,
  get a per-unit three-state verdict (live / check / gone) with the reason,
  an admissible price with its layer, and a screenshot. Requires
  `pip install playwright && playwright install chromium` once.
- `scripts/extract_embedded.py` - pulls listing fields from a page's
  embedded JSON (`__NEXT_DATA__`, ld+json, `window.NAME =`) before any DOM
  scraping fallback.
- `scripts/gates.py` - field-level provenance helpers and tri-state
  (PASS/FAIL/UNKNOWN) hard-gate evaluation; `grade_fields` is the
  anti-fabrication grading pass.
- `scripts/checked.py` - hunt memory (`data/checked.json`): rejections
  keyed to a criteria fingerprint, deferred pools, out-of-window tokens,
  per-run scan coverage, and the drain-as-you-go candidate file.
- `scripts/feed_refresh.py` - feed-first price refresh: diffs a full
  token -> price feed against tracked units, plans individual checks for
  missing ones (all up to 15, else a reported sample), and applies
  verdicts so only a confirmed `gone` delists a unit.
- `scripts/dedupe.py` - same-unit matching on coordinates + floor + beds
  (gone units included) and the leverage each duplicate reveals.
- `scripts/quality.py` - data-quality traps (unstated rent, shared rooms
  and sublets, below-floor bait, implausible sizes, placeholder fees,
  stale and bumped ads), billing-period fee normalization, publication
  freshness without invented timezones, the relative-time notes sweep,
  and the pre-ship integrity report.
- `scripts/doctor_searches.py` - expands the `references/platforms.md`
  recipes into a `saved_searches` block and preflight-checks every URL in
  `config.yml` still resolves and renders results.
- `scripts/snapshots.py` - the append-only price/liveness ledger: run-to-run
  diffs, price-drop detection, per-unit trajectories, the weekly digest, and
  the heartbeat run log.
- `scripts/flood.py` - keyless FEMA NFHL flood-zone lookup for a building's
  lat/lon.
- `scripts/scoring.py` - turns per-unit dimension scores into a composite,
  action band, distress flags, and concession climate against
  `scoring.yml`.
- `scripts/costs.py` - deterministic true monthly cost, total cost of
  ownership, 40x-income qualification, and renewal-vs-relocate math.
- `scripts/backlog.py` - appends `NEW` entries to `actions.yml` (a text
  append, never a rewrite - it never touches the user's existing notes) and
  builds the hydrate report's resurfacing backlog section.
- `scripts/lease_dates.py` - pure date math for lease critical dates
  (renewal notice, concession reversion, deposit return).
- `scripts/photo_hash.py` - a perceptual-hash scam net across archived
  listing photos (recycled photos, price-gap flips, zombie reposts).
- `scripts/walk.py` - Walk Score lookup for a building address, cached,
  with n/a on any failure.
- `scripts/shortlist.py` - shortlist-sync rows, the sync plan, the
  live-link gate, and the read-back diff for `apartmentops-sync`.
- `references/platforms.md` - per-platform saved-search recipes, markers,
  walls, and quirks (dated observations).
- `scripts/line_advisor.py` - suggests same-line sibling units in a tower
  from a hand-curated per-building line map.
- `references/collection-playbook.md` - feed-first scanning, drain as you
  go, pagination, item-page probing, hidden-tab timer throttling, bot-wall
  handling, hunt memory, refresh and removal rules, dedupe leverage,
  quality traps, and note hygiene.
- `references/provenance.md` - the field-level provenance shape, tri-state
  gate rules, and how `constraints.json` plugs into grading.
- `references/constraints.json` - the machine-readable anti-fabrication rule
  set (see Ground rules above).
- `references/ledger.md` - `snapshots.jsonl` / `run-log.jsonl` schemas, the
  `run_id` convention, and the heartbeat contract.
- `references/scoring.md` - the `scoring.yml` spec: dimensions, verbal
  anchor bands, the move-in-window-fit formula, distress and climate rules.
- `references/costs.md` - the cost-math contract: `inputs.json`/
  `results.json` shapes, and the rule that income never touches disk.
- `references/actions.md` - the `actions.yml` / `backlog-state.json`
  ownership split and the resurface-max-3-then-stale rule.
- `references/lease-fields.md` - the field dictionary `apartmentops-lease`
  extracts against.
- `references/line-substitution.md` - the per-building line-map schema and
  the trailing-letter trust rule.
- `assets/example-dashboard.html` - a finished example dashboard to study
  before building one (structure, map projection, grade chips, link rows).
  Its buildings, units, prices, grades and commutes are synthetic, generated
  by `assets/make_example_data.py` from one fixed seed; only the map
  geography (shorelines, stations) and the transit fare table in the footer
  are real.

The repo's `tests/` directory (root level, alongside this skill tree) covers
every script above except `geocode.py` (a live Nominatim call with no
offline test) - run `pytest` from the repo root before trusting a change to
any of them.
