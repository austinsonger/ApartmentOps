# ApartmentOps snapshot ledger and delta engine

Market memory for the pipeline: every hydrate run appends what it observed
to a permanent, append-only ledger, and a small set of pure-local
computations read that ledger back as diffs, drops, trajectories, and a
rendered digest. All logic lives in
`.claude/skills/apartmentops/scripts/snapshots.py` (stdlib only, no network
calls). This file documents the two plain files it writes, the run_id
convention, the heartbeat contract, and the loud-error rule the module
enforces on purpose.

Both files below live under `apartmentops/` in the user's project root,
alongside the files documented in `contracts.md`. They are plain files, not
a database - per the project's data contract, the ledger is meant to be
readable with `cat` and diffable with `git`.

## apartmentops/data/snapshots.jsonl (written by: hydrate, one row per unit per run)

One JSON object per line, newline-delimited (JSONL). Appended to only -
never rewritten, never compacted, never sorted in place. Downstream reads
(diffs, drops, trajectories) tolerate any file order because every
computation groups and sorts by `run_id` / `observed_at` itself.

```json
{"unit_id": "tower2-2207", "url": "https://example.com/tower2/2207",
 "observed_at": "2026-07-13T14:02:11-04:00", "run_id": "hyd-20260713-1402",
 "price": 2450, "availability": "2026-08-01", "status": "live",
 "fetch_evidence": "apartmentops/shots/tower2-2207.png"}
```

| Field | Type | Notes |
|---|---|---|
| `unit_id` | string | Same key used for screenshots elsewhere in the pipeline, e.g. `"tower2-2207"`. Not enforced to a specific format, just required and non-empty. |
| `url` | string | The URL re-checked this run (normally `unit_deep_link` from `verified.json`). |
| `observed_at` | string | ISO 8601 with a timezone offset. Required on every row; used to order runs and compute windows. |
| `run_id` | string | See "run_id semantics" below. Every row appended together in one `append_rows()` call must carry the identical run_id. |
| `price` | number or null | Monthly USD, no strings, no "$". `null` when this re-check could not confirm a price - never a guess, never the previous run's value carried forward. |
| `availability` | string or null | Whatever the page states (a date, "Immediate", etc.) or `null` if unconfirmed. |
| `status` | `"live"` \| `"gone"` \| `"recheck"` | The fetch outcome for this row. `"recheck"` means the re-check itself was inconclusive (timeout, blocked, ambiguous page) - not that the unit is confirmed live or gone. |
| `fetch_evidence` | string or null | Path to the screenshot hydrate already captures for this check, or a short note. Points at existing evidence; this ledger never triggers a new fetch of its own. |

**Append-only rule.** `append_rows(path, run_id, rows)` opens the file in
append mode only. It validates every row in the batch before writing
anything, so one malformed row aborts the whole append rather than
corrupting the file with a partial batch. A second hydrate run never
touches a line written by an earlier run - if you need to see history,
read the file; nothing here rewrites it.

**Never guessed.** A field the re-check could not confirm is `null`, with
`status` carrying the fetch outcome. This module will never write a value
it did not directly receive from the caller - it does not interpolate, it
does not copy a prior run's price forward, and it does not infer a status.

## run_id semantics

`run_id` is an explicit identifier generated once at the start of a
hydrate run, before the first unit is re-checked, and threaded through
every row that run appends. A convention like `hyd-YYYYMMDD-HHMM` (for
example `hyd-20260713-1402`) is recommended but not enforced by the
module - what matters is that the caller commits to one string per run and
stamps it consistently.

This is a deliberate fix. The pattern this ledger replaces reconstructed
"which rows belong to the same run" by grouping on shared timestamp
seconds, which breaks the moment a run is slow, retried, or overlaps
another. `load_runs()` and `rows_for_run()` both key off the explicit
`run_id` field - never off timestamp proximity - so a slow run, a retried
unit, or a run that spans midnight all still group correctly.

## apartmentops/data/run-log.jsonl (written by: hydrate, one row per run - the heartbeat)

One JSON object per line, appended by `append_runlog()` at the end of
**every** hydrate run, including a run where nothing changed:

```json
{"run_id": "hyd-20260713-1402", "at": "2026-07-13T14:03:40-04:00",
 "units_checked": 24, "live": 21, "gone": 2, "recheck": 1, "changes": 0}
```

| Field | Type | Notes |
|---|---|---|
| `run_id` | string | Same run_id stamped on that run's snapshot rows. |
| `at` | string | ISO 8601 with timezone; when the run-log row itself was written. |
| `units_checked` | int >= 0 | Total units re-checked this run. |
| `live` | int >= 0 | Count of rows this run with `status == "live"`. |
| `gone` | int >= 0 | Count of rows this run with `status == "gone"`. |
| `recheck` | int >= 0 | Count of rows this run with `status == "recheck"`. |
| `changes` | int >= 0 | Count of new + removed + price-changed units this run produced (0 on a quiet run - still written). |

### Heartbeat contract

**A missing weekly row means the scheduler died - the absence of the row
is the alarm, not its content.** Hydrate must call `append_runlog()`
unconditionally at the end of every run, whether or not anything changed.
`heartbeat_overdue(runlog_path, max_age_days, now=None)` returns `True`
when the newest row is older than `max_age_days`, or when the file has no
rows at all (no heartbeat ever recorded is the most overdue state there
is). The dashboard freshness banner (a later ticket) keys directly on this
boolean: a red banner means "the scheduler is not running", never "the
market is quiet" - that distinction only holds if this row is written on
every single run, including zero-change ones.

The source project this epic replaces sent an always-on email digest
instead. That is deliberately not adopted here: nothing in this ledger, or
anywhere in ApartmentOps, sends a message on the user's behalf. The digest
and the heartbeat exist only as rendered Markdown and a plain JSONL file.

## The loud-error rule

`diff_last_two(path)` requires at least two distinct `run_id` values in
the ledger. With fewer than two, it raises `InsufficientHistoryError` with
the exact message:

```
insufficient history: N run(s) recorded, 2 required
```

This is intentional and must never be caught and swallowed into an empty
diff. The failure mode this fixes: a scheduled consumer that reads `{}`
(or all-empty lists) cannot tell "checked, nothing changed" apart from
"there isn't enough data to check yet" - and a user who sees a quiet
"no changes" digest on week one, before any comparison was even possible,
will trust a claim the system never actually verified.

`digest_markdown()` is the one place that catches this exception - because
a rendered artifact must always show *something* - and it renders the
exact same message inline instead of an empty digest. Every other caller
(the CLI's `diff` subcommand, the run report, any future integrator) should
let the exception propagate or explicitly catch and surface it; catching
and discarding it is the bug this module exists to prevent.

The same "loud instead of silent" posture applies to `detect_drops()`:
units with fewer than two priced observations are not silently omitted -
they are counted in the returned `insufficient_history` tally, which the
digest surfaces so an empty drops list is never mistaken for full
coverage.

## Price-drop thresholds (config.yml)

The percent and window thresholds are user preferences, not constants, per
the ticket. This module does not read `config.yml` itself (kept stdlib
and dependency-free) - the caller reads the `ledger:` block and passes the
values through as parameters or CLI flags. Recommended config shape and
documented defaults (matching this module's own defaults):

```yaml
ledger:
  drop_pct: 5.0          # flag a unit that fell at least this many percent
  drop_window_days: 14   # ...within this many days
```

If the block is absent, integrators should fall back to
`snapshots.DEFAULT_DROP_PCT` (5.0) and `snapshots.DEFAULT_DROP_WINDOW_DAYS`
(14), which are the same numbers `detect_drops()` and `digest_markdown()`
use when no override is passed.

## Function reference

All pure-local, stdlib-only, no network calls. Import directly from
`snapshots.py` (tests and other scripts already do this via
`tests/conftest.py`'s path hook).

| Function | Purpose |
|---|---|
| `append_rows(path, run_id, rows)` | Validate and append snapshot rows. Returns count appended. |
| `append_runlog(path, run_id, stats, at=None)` | Append one heartbeat row. `stats` needs `units_checked`, `live`, `gone`, `recheck`, `changes`. |
| `load_runs(path)` | Every distinct `run_id`, oldest first. |
| `rows_for_run(path, run_id)` | All rows for one run, in file order. |
| `sanity_check(path)` | Non-raising integrity check for the run report: total rows, parse errors, rows missing `run_id`. |
| `diff_last_two(path)` | NEW / REMOVED / PRICE_CHANGED / price_unknown between the two most recent runs. Raises `InsufficientHistoryError` below 2 runs. |
| `detect_drops(path, min_pct, window_days, now=None)` | Windowed price-drop detector with cited evidence; see the function docstring for the exact window semantics. |
| `trajectory(path, unit_id)` | Ordered `[{run_id, observed_at, price, status}]` series for one unit - the raw material for sparklines. |
| `digest_markdown(path, now=None, drop_pct=..., drop_window_days=...)` | Renders the full "What changed this week" Markdown section. Never raises. |
| `heartbeat_overdue(runlog_path, max_age_days, now=None)` | `True` when the run-log's newest row is stale or absent. |

## CLI

```
python3 snapshots.py append --run-id hyd-20260713-1402 [--target snapshots|runlog] [--path FILE] < payload.json
python3 snapshots.py diff [--path FILE]
python3 snapshots.py drops [--min-pct 5] [--window-days 14] [--now ISO8601] [--path FILE]
python3 snapshots.py digest [--drop-pct 5] [--window-days 14] [--now ISO8601] [--path FILE]
python3 snapshots.py runlog-check [--max-age-days 10] [--now ISO8601] [--path FILE]
```

`append --target snapshots` reads a JSON array of rows from stdin.
`append --target runlog` reads a single JSON stats object from stdin.
`diff` exits 1 (and prints the insufficient-history message to stderr)
when there are fewer than two runs - it does not print an empty JSON
object. `runlog-check` exits 1 when the heartbeat is overdue, 0 otherwise,
so it can gate a shell script directly.
