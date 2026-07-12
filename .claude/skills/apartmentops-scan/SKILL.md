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
`apartmentops/data/verified.json`, `apartmentops/shots/*.png`.
Read `../apartmentops/references/contracts.md` for the exact schemas, and
repeat the parent skill's ground rules (anti-fabrication, liveness,
human-in-the-loop, no emojis) verbatim to every subagent - subagents invent
things when the rules stay implicit.

## Phase 1 - Hunt (parallel researchers)

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

Merge results, dedupe on (address + unit), drop rows failing hard gates or
outside the band (allow a small tolerance - sites drift daily), and write
candidates.json. Log what was dropped and why; silent truncation reads as
"covered everything" when it did not.

## Phase 2 - Verify (headless browser; evidence or it did not happen)

For each candidate (rank by fit, cap sensibly, loop batches until the target
count is confirmed): load `verify_url` in headless Chromium - the bundled
`../apartmentops/scripts/verify_units.py` does the mechanical check (token
present, nearby price, screenshot), or drive Playwright directly when a page
needs interaction. Confirm:

1. **Live today** - the unit is listed available NOW. Reject stale, archived,
   or syndicated ghost listings; a zombie listing from years ago looks
   identical to a real one until you check the page's own dates.
2. **Price** - as shown on the page, classified net vs gross.
3. **Per-unit deep link** - harvest hrefs containing the unit number; store
   the deepest one. Index pages paginate and lazy-load, so a later re-check
   against an index produces false "gone" verdicts; the deep link is the
   durable handle.
4. **Screenshot** to `apartmentops/shots/` - permanent evidence that this was
   live on this date, since the link itself will outlive the unit.
5. **Scam screen** - price far below the building's own comps, no-deposit
   tells, mismatched addresses. Flag, do not silently drop.

Watch for unit-number collisions: two towers in one complex can both have a
unit with the same display number at different prices. Trust feed IDs over
display names.

Write verified.json (candidates plus verification fields). Report to the
user: counts (live, rejected, gone), the standouts against their gates, and
price movements if this is a re-scan. Then offer the next stage:
`apartmentops-research` if areas.json does not exist yet, otherwise
`apartmentops-dashboard`.
