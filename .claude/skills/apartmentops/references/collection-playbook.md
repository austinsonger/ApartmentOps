# Collection playbook: feed-first scanning, hunt memory, and refresh

How the scan stage collects listings without burning its access, losing
work to a bot wall, or mistaking an incomplete scan for an empty market.
These are mechanisms learned from real hunting rounds; platform-specific
details (URL shapes, payload paths) are historical observations to verify
against a live page before relying on them, never guarantees.

Scripts: `scripts/checked.py` (hunt memory + drain file),
`scripts/feed_refresh.py` (feed diff + removal tracking),
`scripts/dedupe.py` (same-unit matching + leverage),
`scripts/quality.py` (data-quality traps, freshness, note hygiene,
integrity). File shapes are in `contracts.md`.

## 1. The order of work

Opening every listing to read its details is what gets a browser blocked.
A search feed hands most fields over for free; use it to shrink the work
before opening anything.

```
1. Feed pages     -> token, area, price, beds/rooms, size, coords  [few requests]
2. Filter INLINE  -> geography, rent band, size, rooms, hard gates, and
                     checked.json (filter inside the collection loop)
3. Diff           -> feed_refresh.diff_feed against verified.json
                     (price changes + possibly-missing units)
4. Open survivors -> one item page each, for dates, fees, advertiser, images
5. Record         -> new units into the pipeline; every rejection and
                     deferral into checked.json
6. Quality, dedupe, grade, gates, verify - then the rest of the scan phases
```

The band filter in step 2 has one exception
(`quality.feed_price_decision`): a card over the ceiling but within
`gross_max_stretch` that carries a concession or starting-at badge is
opened rather than skipped, because the badge hides the true unit rent -
the card shows a teaser or an averaged net, and only the unit's own page
shows what that unit costs.

A realistic round: a few hundred raw results, under a hundred after the
area filter, a handful genuinely new. If the "new" count is in the
hundreds, the filters or the hunt memory failed - stop and look.

## 2. Drain as you go

Browser page state is volatile storage. A CAPTCHA navigates the tab to
another origin without warning, and everything in page variables is gone;
anything staged in the scraped origin's `localStorage` becomes unreachable
too, because you cannot get back to that origin to read it.

- Drain the candidate token list to disk the moment the feed scan ends:
  `checked.drain_append("apartmentops/data/drain/<run_id>.jsonl", rows)`.
- Drain item details in small batches (about five records) while the loop
  is still running. Never let more than about ten undrained items pile up.
- `checked.load_drained()` reads it back, merging feed rows with later
  detail rows per token and tolerating a truncated last line.
- Backups before a destructive change to a data file belong inside the
  project (`apartmentops/data/backups/`), never in a temp or scratch path
  that can be wiped between sessions.

## 3. Pagination and query hygiene

- Read the total page count the source reports on page 1 and loop to it.
  Page size is not fixed (one real feed split 194 results as
  40/40/40/31/20/20/9); never compute a page count from an assumed size.
  Dedupe by token. Record each page with `checked.mark_page`.
- Use the platform's canonical, fully filtered URL (the `saved_searches`
  form). Short forms can redirect and silently drop every filter; the tell
  is a result count in the thousands.
- There is often no working date sort. Freshness comes from the item page,
  not feed order.
- Scan the smallest area first. If a wall lands mid-scan you keep the
  small areas, and a big city's default first page is usually its
  cheapest outlying blocks - the least useful page in the scan.

## 4. Item pages: probe, pace, and browser-tool constraints

Probe before choosing a route: one cheap same-origin request from an
already-loaded page tells you the state.

| Symptom | Meaning | Route |
|---|---|---|
| Returns 200 in well under a second | open | in-page fetches, paced |
| Hangs until the abort timeout | tarpitted | real navigation per item |
| Throws in under a second | fetch layer blocked | navigation, and expect the wall soon |

- Pace with randomized gaps of several seconds and a request timeout.
- Hidden-tab timer throttling: an automation tab is usually hidden, and
  browsers clamp `setTimeout` there, so a 6-second pause can silently run
  at one to three minutes per item and look like server throttling. Check
  `document.visibilityState`; pace with a task-based wait instead:

  ```js
  const sleep = ms => new Promise(res => {
    const t0 = Date.now(), mc = new MessageChannel();
    mc.port1.onmessage = () => { Date.now() - t0 >= ms ? res() : mc.port2.postMessage(0); };
    mc.port2.postMessage(0);
  });
  ```

- Some in-browser JavaScript tools return `{}` for a promise. Run async
  work as `window.__go = async () => {...writes results to window...}`,
  call it, then read results in a separate synchronous call.
- Tool output can truncate around a thousand characters. Return a few
  records per call in a compact delimited encoding, never pretty JSON.
- A heavy results page can freeze the renderer. Close the tab, open a
  fresh one, and split a large injection into smaller calls.
- Only use browser capabilities the current tool documents. Do not run
  arbitrary page scripts or navigation methods a tool does not permit.

## 5. When the wall appears

Escalation looks like: item requests slow or throw, then a bot-manager or
CAPTCHA page, then search pages blocked too.

- Never solve or bypass a CAPTCHA or bot wall. Say so plainly; only the
  user can clear it in their own browser.
- Record it: `checked.add_blocker(state, run_id, "captcha", detail, at)`.
  `checked.coverage()` then reports the scan as partial, and
  `feed_refresh.diff_feed(..., feed_complete=False)` refuses to call any
  unit possibly missing.
- Walls often clear on their own (observed from about twenty minutes to a
  few hours). One or two spaced retries are reasonable; prolonged retry
  loops are not.
- Ship what you drained. A blocked round still reports every verified row
  it has, with the coverage limits stated.
- Before reloading to clear a stuck queue, pull state out first.

## 6. Hunt memory (`checked.json`)

Load it at the filter step; append to it at the record step.

- `checked.criteria_fingerprint(config)` stamps every rejection with the
  criteria it was judged against. `checked.filter_new` re-opens a
  criteria-dependent rejection when criteria change, and any rejection
  older than 30 days (evidence expires). Reasons that hold under any
  search (sublet, scam flag, rent not stated) are recorded with
  `criteria_dependent=False`.
- `deferred` holds candidates filtered before opening (out of area, over
  band). They are the first places to look when the user widens.
- `out_of_window` stops every incremental round from re-fetching the old
  tail. If the dataset is ever rebuilt to hold only a recent window, a
  plain "token not in verified.json" test reports every old excluded
  listing as new - keep `out_of_window` current.

## 7. Refresh and removals

`feed_refresh.diff_feed(feed, units, feed_complete=..., price_ceiling=...)`
compares a full token-to-price feed read (keep EVERY token in this pass,
no discovery filters) with the tracked set.

- Feed absence alone never proves removal. Filters, a price rise above the
  ceiling, an incomplete scan, and access failures all hide live ads.
- `feed_refresh.verification_plan`: verify every missing unit individually
  when there are 15 or fewer (in one real round, 8 of 9 missing were gone
  and one was fully live); above that, sample at least 8 and report the
  ratio. Unsampled units stay `possibly_missing`.
- `feed_refresh.apply_verdicts` applies `verify_units.py` verdicts: only
  `gone` sets `availability_state: delisted` plus the `delisted` date;
  `live` restores `active`; `check` changes nothing. Stamp `price_checked`
  only when a price was actually read.
- A feed price is a pointer, not an admissible price: confirm a change on
  the unit's own page before writing `rent_verified`.
- Listings move fast (roughly half gone within a week in one tight
  market). Re-verify prices every round, and report the strongest verified
  find in the same message as the round - with its link and verified
  date - rather than holding it for later.

## 8. Same-unit detection is leverage

`dedupe.find_matches(new, tracked)` keys on coordinates + floor + beds,
not on the address string (hand-typed house numbers are often blank or off
by one or two), and compares against gone units too. Only `token` and
`strong` matches auto-merge; a `candidate` (same building cell, no unit
number corroboration) is kept for review, never merged.
`dedupe.classify_group` names what a duplicate means: a same-agent price
gap to quote, a relist that never rented, a real price cut, an owner ad
that avoids a broker fee (keep it as primary), and size growth or feed
flooding that discredits an advertiser's numbers. `dedupe.choose_primary`
keeps the other ads as `alternative_sources` with their prices and URLs.
Negotiation readings are inferences, never proof of owner pressure.

A cluster of same-address broker ads appearing together is usually new
construction marketed by whoever got a listing, not spam. Log the
building; revisit if prices drop.

## 9. Data-quality traps and notes

`quality.traps(unit, config, comps, now)` returns flags with an action
(`skip` / `no_star` / `warn` / `info`); see the module docstring for the
full list. `no_star` units never reach TourNow and never earn a value
badge, whatever their score.

Notes are the product - they turn a listing into a decision. Every note
should carry the true monthly cost when fees are known
(`quality.known_monthly_total`, which normalizes each fee by its billing
period and never adds a bi-monthly or annual bill as a monthly one),
negotiation leverage with cited evidence, what is unverified, and
location problems the data does not encode (a listing can pass every
filter and sit on a six-lane road; name the street, suggest a rush-hour
visit). When all-in cost exceeds the ceiling, that warning is the FIRST
sentence. Warnings live in `notes`, never in a field a scorer overwrites.

Never write relative time in a note ("posted today", "hurry"). It is true
for one day and wrong forever after. Write the absolute date; the
dashboard's live badge says how fresh it is. `quality.relative_time_hits`
sweeps for these every round. Get the current date from the system, never
from assumption.

Keep `published_at` as the full original publication timestamp with its
known timezone (hour-level freshness changes what the user should do).
Never use a refreshed `updated_at` as publication time; a large gap
between the two is itself a signal (`bumped_listing`). Never invent a
timezone for a naive timestamp.

## 10. Before shipping

`quality.integrity_report(units, config)` checks duplicate ids, missing
local evidence files, out-of-spec live rows, unmerged same-unit pairs,
and relative-time notes. Fix or explain every finding in the run report.
Keep private working files (checked.json, drain files, contact details,
notes meant only for the user) out of anything published.

## 11. Secondary marketplaces

Peer-to-peer marketplaces (for example Facebook Marketplace) are low
yield - roughly one usable listing per full pass in past rounds. Run the
primary platforms first. Read-only: never message a seller or post
anything. They usually give only vague times ("listed over a week ago");
record that in notes and leave `published_at` unset.
