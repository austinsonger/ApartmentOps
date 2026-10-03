---
name: apartmentops-scan
description: >-
  ApartmentOps stage 2 - hunt the market and browser-verify every find. Fans
  out parallel researchers per neighborhood against the user's
  apartmentops/config.yml, then confirms each candidate is live TODAY with a
  real headless browser (screenshot evidence + per-unit deep links) and writes
  apartmentops/data/verified.json. Use this skill whenever the user wants to
  scan for apartments, refresh listings, re-run the apartment search, check
  what is on the market right now, or says things like "find me options",
  "rescan", "any new units this week", or "reverify the listings". Requires
  onboarding (the apartmentops skill) to have produced config.yml first.
---

# ApartmentOps: Scan (hunt + verify)

Inputs: `apartmentops/config.yml`. Outputs: `apartmentops/data/candidates.json`,
`apartmentops/data/verified.json`, `apartmentops/shots/*.png`,
`apartmentops/extractors/{platform}.yml` (probed lazily, see Phase 2),
`apartmentops/data/photo_hashes.json` (see Phase 4),
`apartmentops/data/checked.json` and `apartmentops/data/drain/<run_id>.jsonl`
(hunt memory, see Phase 0.5). Also appends
`status: NEW` rows to the user-owned `apartmentops/data/actions.yml` (see
Phase 5) - never rewrites it.
Read `../apartmentops/references/contracts.md` for the exact schemas, and
repeat the parent skill's ground rules (anti-fabrication, liveness,
human-in-the-loop, no emojis) - plus the machine-readable rules in
`../apartmentops/references/constraints.json` - verbatim to every researcher
and verifier subagent; subagents invent things when the rules stay implicit.

## Phase 0 - Preflight (saved searches)

Before Hunt, if `config.yml` has a `saved_searches` block, run it through
`../apartmentops/scripts/doctor_searches.py`'s
`check_saved_searches(saved_searches)` (profile -> platform -> area -> url,
or the richer `{url, marker}` form). Put the resulting `{profile, platform,
area, url, ok, status, detail}` rows in the run report as a preflight table
before Phase 1 starts. A saved search is a pre-built, fully-filtered,
newest-first listing URL; where one exists and passes preflight, Phase 1's
researcher for that profile/platform/area navigates straight to it instead
of driving the platform's search UI - filter widgets drift, a baked-in URL
does not. A leg that fails preflight (403 bot wall, non-200, marker miss) is
reported loudly and skipped - never retried against a bot wall, never
silently patched over by falling back to a hand-driven search for that leg.
A `needs_ui` leaf (a platform with no URL template, such as Redfin) is
handled by driving the platform's UI once for that area, caching the URL it
produces into `saved_searches`, and re-running preflight on it.

`saved_searches` can carry more than one profile (e.g. a `fallback_1br`
alongside `primary`, for a secondary configuration). Run each profile as its
own Hunt leg in Phase 1 and tag every row it produces - candidates.json,
verified.json, everything downstream - with that `profile` name, since the
same building can legitimately appear twice under two different profiles at
two different filters.

If `config.yml` has no `saved_searches` block at all, skip this phase
entirely; Phase 1's researchers fall back to driving the platform's search
UI as before, and every row is tagged `profile: "primary"`.

## Phase 0.5 - Hunt memory and the order of work

Read `../apartmentops/references/collection-playbook.md` before the first
scan of a session; it is the field manual for collecting without losing
work or access.

- Generate one `run_id` (`scan-YYYYMMDD-HHMM`) and load
  `apartmentops/data/checked.json` with `checked.load`. Compute
  `criteria = checked.criteria_fingerprint(config)` and call
  `checked.start_scan`. Get the current time from the system, never assume
  it.
- Feed first: read every result page of each saved search (recording
  `set_pages_expected` from the page count the source reports and
  `mark_page` per page), keep the FULL token -> price map for refresh,
  and filter inline by geography, band, size, hard gates and
  `checked.filter_new(tokens, tracked_tokens, state, criteria, now)`,
  where tracked tokens include gone units. Record out-of-area and
  out-of-band tokens with `checked.record_deferred`.
  The band check calls `quality.feed_price_decision(card_price, card_text,
  config["budget"])`: `open` decisions (a concession or starting-at badge
  over `gross_max` but within `gross_max_stretch`, or no price on the card)
  carry `quality_flags: ["concession_badged_over_ceiling"]` into the drain
  row when the reason is the badge; `skip` decisions go to
  `checked.record_deferred(state, "out_of_band", ...)` as today. The same
  inline filter calls `quality.keyword_filter(card_title + " " +
  card_blurb, config.get("filters"))`; a `skip` is recorded with
  `checked.record_rejection(..., criteria_dependent=True)` and its reason
  string (`exclude_keyword:<word>` or `no_include_keyword`).
- Drain as you go: `checked.drain_append` the surviving tokens the moment
  the feed scan ends, and item details in batches of about five while
  opening them. Never hold results only in page variables or the scraped
  site's localStorage.
- Open only the survivors. On a CAPTCHA, bot wall, or repeated throttling:
  stop, `checked.add_blocker`, ship what was drained, and report coverage
  from `checked.coverage` - never a complete-scan claim without it.
- Every listing opened and dismissed gets `checked.record_rejection` with
  its reason (`criteria_dependent=False` for sublets, scams, unstated
  rent). Save with `checked.save` at the end, and after each batch on long
  runs.
- On a re-scan, pass the full feed map to
  `feed_refresh.diff_feed(feed, verified_units, feed_complete=coverage
  ["complete"], price_ceiling=budget.gross_max)`, verify missing units per
  `feed_refresh.verification_plan`, and apply verdicts with
  `feed_refresh.apply_verdicts` - absence from a feed never marks a unit
  gone. Record the round with `checked.record_price_refresh`.

## Phase 1 - Hunt (parallel researchers)

Owner feeds first: before the cluster researchers, spawn one `owner`
researcher per area, scoped to the by-owner saved searches from Phase 0
(Craigslist `apa`, Apartments.com FRBO, and HotPads by-owner when its leg
passed preflight; never when walled). Owner ads go fastest and skip the
broker fee (`../apartmentops/references/collection-playbook.md` section
12). Its prompt carries the note that a missing property or building name
is normal on these feeds, not a red flag, and these ground rules verbatim
(from `../apartmentops/SKILL.md`):

> - **Anti-fabrication:** report only facts tied to a URL actually fetched or a
>   file actually read. UNKNOWN is always an acceptable value. Never invent
>   unit numbers, rents, years, orientations, or safety claims.
> - **Constraints travel verbatim, and outputs get graded:** every subagent
>   prompt carries `references/constraints.json`'s rules verbatim, not
>   paraphrased or summarized. After a stage produces output, grade it with
>   `scripts/gates.py`'s `grade_fields` - autofail on `value-without-provenance`
>   (a FACT/INFERRED value with no source) and `placeholder-left-in-output`
>   (TODO/TBD/FIXME/lorem-ipsum/example.com left in a committed file). A unit
>   that fails grading is withheld and re-checked next run, never shipped with
>   a footnote.
> - **Liveness or it does not count:** a listing must be verifiably live on the
>   day of the check. Stale syndication pages ("zombie listings") look real and
>   are not - years-old listings resurface on syndication pages looking current.
> - **Evidence is per source, not per link type:** an index or root page confirms live but never proves gone; a login wall, bot wall, CAPTCHA interstitial, empty shell, or fetch error keeps the prior verdict and can never overturn a prior gone; a complete, unpaginated operator table that omits a unit IS evidence of gone; a per-unit deep link is admissible only when the source policy says so (some operators render any invented unit id with a price).
>   Prices come only from the unit's own page or its own table row, with the price layer recorded; a price near a unit token on an index is unusable.
>   The policy is the `sources:` list in `apartmentops/sources.yml` (`references/contracts.md`); harvest deep links when found, and record a policy entry for every operator you meet.
> - **Human-in-the-loop:** never submit applications, book tours, send
>   emails/messages to brokers or leasing offices, or click
>   Book/Submit/Confirm/Apply on any site. Produce drafts and links; the user
>   sends. Read-only browsing of public pages only; no logins, no CAPTCHA or
>   bot-wall bypasses; back off on 403s and find another public source.
> - **Absence is not removal:** a unit missing from a search feed is
>   `possibly_missing` (or `unknown` on a partial scan), never gone, until
>   its own surface confirms it. Never call a scan complete without
>   `checked.coverage` saying so.
> - **Absolute dates only:** notes never say "today", "yesterday", or
>   "hurry"; they carry the date. Publication, observation, verification
>   and removal dates stay distinct, and no timezone is ever invented.
> - **Private stays private:** `checked.json`, drain files, contact details
>   and working notes never go into a published dashboard.
> - **Lessons go to the user first:** at the end of a round, report any new
>   reusable mechanism learned (a trap, a broken URL form, a data pattern),
>   without personal data. Edit a skill or reference file only when the
>   user authorizes it.
> - **No emojis in any produced file.**

Spawn one researcher per geographic cluster in `geography.areas` (plus one
broad aggregator sweep and, when coverage feels thin, a completeness critic
that names specifically-missed buildings). Use a Workflow if the user has
opted into multi-agent orchestration; otherwise sequential agents work fine.

Each researcher prompt needs, explicitly:
- The full filter from config (band, beds/baths, gates with hard/bonus modes)
  and the instruction to report gross AND net-effective with the concession
  spelled out. Net prices are year-one deals; the gross is what renewal costs.
- The anti-fabrication and liveness rules, and "your final message is
  machine-consumed data".
- A warning that big listing sites 403 plain fetches: prefer building-direct
  sites and permissive aggregators; never bypass a bot wall - find another
  public source instead.
- The requirement to return a `verify_url` per unit: the best page a headless
  browser could load to re-confirm the unit (a per-unit page if one exists).
- When Phase 0 found a passing saved-search URL for this profile/platform/
  area, hand it to the researcher directly and tell it to navigate straight
  there rather than driving the search UI redundantly.

Merge results, dedupe on (address + unit + profile - the same address can
legitimately appear once per profile) and then on the unit itself with
`dedupe.find_matches(new_rows, tracked_units_including_gone)` (coordinates +
floor + beds, never the address string alone; only `token`/`strong`
matches merge, `candidate` matches go to `duplicate_candidates` for
review). Building-scope NEW classification (Phase 5) happens after this
dedupe, never instead of it: a relisted unit is still merged here first. For each same-unit group, `dedupe.choose_primary` keeps the
no-fee or cheapest live ad as primary with the rest as
`alternative_sources`, and `dedupe.classify_group`'s signals (price gap,
relisted unrented, real price cut, owner vs broker, size growth, feed
flooding) go into the unit's notes as cited evidence. After
`dedupe.find_matches`, run `quality.owner_signals(unit)` on every row and
write the result as the provenance object for `advertiser_type` (FACT
`owner` with the feed URL as source from a by-owner feed, INFERRED from
phrases, MISSING otherwise; an existing FACT is never overwritten). Run
`quality.traps(unit, config, comps, now)` on every row: drop `skip` rows
into `checked.record_rejection`, store the rest as `quality_flags`. Drop
rows failing hard gates or
outside the band (allow a small tolerance - sites drift daily), and write
candidates.json. Tag every row with its `profile` (default `"primary"` when
no `saved_searches` are configured). Log what was dropped and why; silent
truncation reads as "covered everything" when it did not.

## Phase 2 - Verify (headless browser; evidence or it did not happen)

For each candidate (rank by fit, cap sensibly, loop batches until the target
count is confirmed): load `verify_url` in headless Chromium - the bundled
`../apartmentops/scripts/verify_units.py` does the mechanical check (a
three-state verdict live / check / gone with its reason, an admissible price
with its layer, a screenshot; pass `--policy apartmentops/sources.yml`), or
drive Playwright directly when a page needs interaction.
A `check` verdict at scan time means the unit was NOT verified (a wall, shell, index without the token, or fetch error): never write it with `live: true`; re-verify against another public surface or leave it out of verified.json with the reason in the run report.

Before falling back to DOM parsing, read fields from the page's own embedded
payload first: `../apartmentops/scripts/extract_embedded.py`'s
`find_embedded_payloads(html)` locates any `__NEXT_DATA__`, `ld+json`, or
inline `window.NAME = {...}` block on the page, then `extract_fields(payloads,
spec)` pulls named fields out of it via the per-platform spec at
`apartmentops/extractors/{platform}.yml` (shape: `examples/extractor.example.yml`).
The first time this skill meets a platform it has no spec for, bring one up
with a one-time empirical probe against one or two known-live listing pages:
check in the order above for a payload, record which kind was found (or
`none` - a recorded "no embedded payload" is a valid, useful outcome, not a
failure), record the dot-paths that actually resolved, and write the spec
file - never guess a path you have not observed on a real page. Re-run the
probe and overwrite the spec (bumping `probed_at`) whenever a platform's
extractions start coming back MISSING; a path miss usually means the
platform changed its bundling, not that the field vanished. DOM parsing
remains the fallback for any field the embedded payload does not carry, and
the screenshot is still mandatory regardless of extraction method. Tag each
field's extraction method (`"embedded:next_data"`, `"embedded:ld_json"`,
`"embedded:window_state"`, or `"dom"`) in the `extraction` block written to
verified.json - see Phase 3 for the full provenance shape this feeds.

Confirm:

1. **Live today** - the unit is listed available NOW. Reject stale, archived,
   or syndicated ghost listings; a zombie listing from years ago looks
   identical to a real one until you check the page's own dates.
2. **Price** - as shown on the page, classified net vs gross.
3. **Per-unit deep link and source policy** - harvest hrefs containing the unit number and store the deepest one; it is the durable handle for hydrate.
   Then record what that link can prove in the `sources:` list of `apartmentops/sources.yml` (shape in `../apartmentops/references/contracts.md`): an index page confirms live but never proves gone (it paginates and lazy-loads), a complete unpaginated operator table that omits a unit is gone evidence, and an operator whose per-unit page renders any invented unit id with a price gets `kind: untrusted` so its index, not its deep link, is authoritative.
   Prices are admissible only from the unit's own page or its own table row; note the price layer (net-effective asterisk, base rent, total monthly) per source.
4. **Screenshot** to `apartmentops/shots/` - permanent evidence that this was
   live on this date, since the link itself will outlive the unit.
5. **Scam screen** - price far below the building's own comps, no-deposit
   tells, mismatched addresses. Flag, do not silently drop. On owner ads,
   also flag the owner-specific tells as `scam_flags` text: a deposit by
   wire or gift card only, a refusal to show the unit in person, and a
   price far below same-bed comps (`quality.traps`' `far_below_comps`
   already raises the price tell).

Watch for unit-number collisions: two towers in one complex can both have a
unit with the same display number at different prices. Trust feed IDs over
display names.

## Phase 3 - Provenance, gates, and grading

Shape every freshly-extracted field per
`../apartmentops/references/provenance.md` before it goes near
verified.json: FACT needs a `source` URL and `evidence` (screenshot path or
verbatim quote); INFERRED needs `confidence` and collapses to MISSING below
0.3; MISSING is `value: null`, never a guess and never carried forward from
a prior run; CONFLICT keeps `value: null` and lists the competing
observations in a `conflicts` array rather than picking a silent winner.
Pre-existing bare-scalar data stays legal (read as FACT/source-null or
MISSING via `gates.normalize_field`) - this shape is for what this run
adds. Fold `../apartmentops/references/constraints.json`'s rules into every
researcher and verifier subagent prompt verbatim, same as the ground rules
quoted above - anti-fabrication has to be spelled out explicitly or it
erodes.

Before writing verified.json, grade the merged output with
`gates.grade_fields(data, constraints)` (`../apartmentops/scripts/gates.py`,
`constraints` being the parsed `constraints.json`). Any autofail
(`value-without-provenance`, `placeholder-left-in-output`,
`grade-without-source`, `price-not-number`, `unverified-marked-live`) blocks
that unit's write - fix the offending field or leave the unit out and
re-verify it next run; never ship an autofailed record. Log withheld units
and their rule ids in the run report.

Build `config_gates` from `config.yml`'s `gates:` block per the translation
table in `references/provenance.md` (hard-mode entries only - bonus-mode
gates like `floor_min`/`sqft_min` feed the scoring dimensions later, not
this tri-state block), then evaluate with
`gates.evaluate_gates(config_gates, unit)` -> `{gate_name: "PASS"|"FAIL"|
"UNKNOWN"}`. UNKNOWN is not a rejection: a unit whose hard gates include any
UNKNOWN stays in the pipeline, with its `gates` block and
`gates.verify_checklist(gate_results, unit)` recorded on the record - never
dropped, never guessed into a PASS. `gates.tour_now_blocked(gate_results)`
is what the research stage reads to decide whether a unit can reach the
TourNow action band; this skill's job stops at recording accurate tri-state
gates, not at assigning the band itself. Write verified.json once grading
and gate evaluation are done.

## Phase 4 - Photo scam net

After verification, build or refresh `apartmentops/data/photo_hashes.json`.
Assemble a manifest row per archived photo - `{unit_id, address, platform,
price, live, photo_path}`, `photo_path` drawn from this run's
`apartmentops/shots/*.png` and any other archived listing photos on file -
and run it through `photo_hash.build_index(manifest)`
(`../apartmentops/scripts/photo_hash.py index manifest.json` from the CLI).
An unreadable photo gets `ahash`/`dhash: null` plus an `error` string and
stays in the index rather than being dropped. Then run
`photo_hash.find_matches(index)` (`photo_hash.py scan photo_hashes.json`)
and map every match's flags (`cross_address`, `cross_platform`, `price_gap`,
`zombie_repost`) onto the matched units' `scam_flags`, carrying the evidence
pair - both units' `photo_path`, `address`, `platform`, `price`, recovered
by joining `a_unit`/`b_unit` back through the index - so a human sees what
matched, not just a bare flag name. A unit absent from
`photo_hash.photo_coverage()` has no archived photos at all; render "photo
check: n/a" for it in the run report and downstream, never a default clean
bill of health.

## Phase 5 - Actions sync

After verified.json is written, sync the user-owned actions tracker.
`backlog.load_verified_units("apartmentops/data/verified.json")`
(`../apartmentops/scripts/backlog.py`) returns the unit set keyed by
`unit_id` regardless of whether verified.json is stored as an array or a
dict, so use it rather than hand-deriving keys. Then:
`before = backlog.load_actions("apartmentops/data/actions.yml")` (empty if
the file does not exist yet), `after = backlog.sync_new_units(before,
verified_units, now)` with `now` a plain `YYYY-MM-DD` date (not a
timestamp - a human edits this file by hand), then
`backlog.append_new_entries("apartmentops/data/actions.yml", before,
after)`. This appends only `status: NEW` rows for units newly seen this
run - a text append, never a parse-and-rewrite - so a hand-set status, a
hand-written note, and any comments already in the file survive byte-for-
byte. This is the only write this skill ever makes to actions.yml; every
other field belongs to the user. Use `backlog.describe_sync(before, after)`
for the run-report line ("actions.yml: N NEW entries appended" or
"actions.yml: no changes").

After `sync_new_units`, classify the new units:
`classes = backlog.classify_new_units(new_units, tracked,
config.get("report", {}).get("new_unit_scope", "unit"))`, where
`new_units` are the units appended this run and `tracked` is every unit
already on file, live or gone. Write each class onto its row in
verified.json as `report_class`, pass `classes` to
`backlog.describe_sync(before, after, classes)`, and report both counts
(new units, and new units in tracked buildings).

Before the report, run `quality.integrity_report(units, config)`,
`quality.relative_time_hits(units)` and
`quality.net_without_gross_hits(units)`; fix duplicate ids, missing evidence
files, out-of-spec rows, relative-time notes, and net figures written
without their gross, or name what remains.

Report to the user: the strongest verified new find FIRST, with its link,
verified date and true monthly cost (listings go fast; a find held for the
next round is often a find lost - when `list_purpose` is `call_first`,
frame it as call now), then the Phase 0 preflight table (which saved
searches passed or failed and why), scan coverage from `checked.coverage`
(pages read vs expected, blockers), counts (new, updated, live, owner ads, rejected,
confirmed delisted, possibly missing / unverified), any units withheld
by an autofail this run, any `scam_flags` raised by the photo net, the
actions.yml sync line, "opened on concession exception: N, of which M
verified in band" (M is those whose verified rent is within `gross_max`;
the rest carry the `over_budget_all_in` warn), the standouts against their gates, and price
movements if this is a re-scan. Then offer the next stage:
`apartmentops-research` if areas.json does not exist yet, otherwise
`apartmentops-dashboard`.
