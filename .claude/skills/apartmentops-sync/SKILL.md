---
name: apartmentops-sync
description: >-
  ApartmentOps stage 6 (optional) - keep the user's shortlist document in
  step with verified units. Appends rows for newly verified, still-wanted
  units to a Google Sheet (or a table in a Google Doc) through a connected
  document connector, refreshes Link cells whose best link changed, and
  never writes a link it has not opened live and judged direct-to-unit.
  User columns (reviews, notes, ratings) are never touched. Use when the
  user says "sync my shortlist", "update the sheet", "add the new units to
  my doc", or when shortlist_sync is enabled and a scan found new units or
  the sync cadence has passed.
---

# ApartmentOps: Shortlist sync

Inputs: `apartmentops/config.yml` (`shortlist_sync` block),
`apartmentops/data/verified.json`, `apartmentops/data/buildings.json`,
`apartmentops/data/scores.json`, `apartmentops/data/actions.yml`.
Outputs: rows in the user's shortlist document, the transient
`apartmentops/data/shortlist-plan.json`, `apartmentops/data/shortlist-state.json`
(recovery counters), and `shortlist_sync.last_shortlist_sync` in config.yml
(the one field under `shortlist_sync` the pipeline writes). Schemas in
`../apartmentops/references/contracts.md`; the deterministic half is
`../apartmentops/scripts/shortlist.py`.

The default target is a Google Sheet (`target.kind: sheet`) written through
the Google Drive connector; a table in a Google Doc (`target.kind:
doc_table`) uses the same plan. Which connector writes is the user's
choice: use only a connector the host has attached and the user has
named in `shortlist_sync.connector`.

Never written to the document: contact details, `checked.json`, drain
data, scam-screen notes meant only for the user, or anything from
`actions.yml` beyond the exclusion it drives.

Ground rules, repeated verbatim to every subagent this skill spawns (from
`../apartmentops/SKILL.md`):

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

## Phase 1 - Preflight

Stop and report "shortlist sync: blocked" with the reason when any of
these fail: `shortlist_sync.enabled` is true; `shortlist.validate_columns(
columns)` passes (it names a missing `address` or `link` column, or a
repeated field or label); the named connector is attached; the target
document id or URL is set. Never fall back to another connector or create
a new document on your own.

## Phase 2 - Discover

Read every table in the document (a Sheet's tabs, or a Doc's tables). Map
each table's header labels to the configured `columns` by label. The main
table is the one whose headers contain every configured label; stop and
report when none does, or when more than one does (ambiguous). Collect
`doc_rows = [{key, row_index, link}]` across ALL tables, keying each row
with `shortlist.norm_address(address cell)` - a unit the user moved to a
"Toured" or "Archive" tab is still in the document and must not be
appended again.

## Phase 3 - Plan

`rows = shortlist.build_rows(verified_units, buildings, scores, actions,
columns)` (live units only, Rejected and Skipped excluded, user columns
blank), then `plan = shortlist.plan_sync(doc_rows, rows,
last_shortlist_sync, now, min_days_between_syncs)` with `now` from the
system clock. Write the plan to `apartmentops/data/shortlist-plan.json`
before any document write; it is the expected side of the read-back diff.
When `plan.run` is false, report `plan.reason` and stop.

## Phase 4 - Gate every link

Every link in `to_append` and `to_refresh` is opened live before it is
written. Do it in a sub-agent that receives only `{address, url}` pairs,
the rubric below, and the ground rules above - no other unit data. For
each pair it returns `{final_url, status, page_text_excerpt,
availability_signal, whole_unit, unit_level}` read from the page itself
(never inferred), and `shortlist.link_gate(row, evidence)` decides:

1. Live: HTTP 200 and the final URL is not a search or results page.
2. Address: street number and street name on the page or in the URL.
3. Active: the page shows the unit as available.
4. Whole unit: not a room, a shared unit, or a sublet.
5. Direct to unit: the unit's own page, not a building or floor-plan page.

`FLAG` (number matches, street name abbreviated) is written and listed in
the report. A `FAIL` labelled `non_direct` is recovered by finding the
unit-level link on the same operator's site; `dead` by finding a live
relisting of the same unit elsewhere. Each attempt first calls
`shortlist.recovery_budget(state, key)` (state from
`shortlist.load_state("apartmentops/data/shortlist-state.json")`, saved
after each attempt); when it returns false, hold the row - mark it
`held` with the reason, never write it. A row with no link at all is held.
Do not write anything until `shortlist.gate_summary(verdicts)["ready"]`
is true.

## Phase 5 - Write

Refresh the Link cell on each `to_refresh` row that passed. Append
`to_append` rows that passed (or flagged), in configured column order,
with every user column blank. `off-market (reviewed)` is written only
when `shortlist_sync.mark_off_market` is true AND the user has already
reviewed that row (its review column is non-empty); nothing else about an
existing row is ever edited. Write through the connector only; no
browser-driven edits of the document.

## Phase 6 - Read back

Re-read the document and run `shortlist.readback_diff(expected_rows,
observed_rows)` against the written rows from the plan; it must be empty
(every planned cell landed, and it compares machine fields only so the
user's columns are never second-guessed). Reopen every link changed this
run once more. Only then stamp `shortlist_sync.last_shortlist_sync` with
the current ISO timestamp. A non-empty diff is reported verbatim and the
stamp is not written.

## Phase 7 - Report

Appended, refreshed, held, and unresolved counts; every held row with its
reason (dead, non_direct after three recovery attempts, no link); every
FLAG row; the document link. Never claim a sync is complete while the
read-back diff is non-empty.
