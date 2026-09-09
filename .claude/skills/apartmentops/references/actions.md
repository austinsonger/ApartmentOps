# apartmentops/data/actions.yml and backlog-state.json

Two files, deliberately split by ownership. `actions.yml` is USER-OWNED: it
records what the user has done about each unit. `backlog-state.json` is
SYSTEM-OWNED: it records how many times the backlog resurfacing feature has
already nagged the user about a unit. Mixing the two into one file would
make a user's hand-edit collide with the pipeline's bookkeeping, so they are
kept apart.

Both files are read and written by
`.claude/skills/apartmentops/scripts/backlog.py`. Read that script's module
docstring for the full function list; this file documents the on-disk
shapes and the ownership rules.

## apartmentops/data/actions.yml  (USER-OWNED)

One entry per unit id, keyed at the top level of the document:

```yaml
example-tower-2-2207:
  status: NEW
  note: ""
  updated_at: "2026-07-15"
example-tower-2-2311:
  status: Contacted
  note: "Emailed leasing office 7/12, waiting on a callback"
  updated_at: "2026-07-12"
```

This is a flat top-level mapping (`unit_id -> record`), not nested under a
`units:` key, so the on-disk shape matches the in-memory dict every
`backlog.py` function reads and writes - no unwrapping step, no ambiguity
about where the real content starts. `backlog.load_actions()` also accepts
a document with a top-level `units:` key wrapping the mapping (an earlier
sketch of this contract used that shape) and unwraps it automatically for
backward compatibility, but anything this codebase writes uses the flat
form.

### Status enum (exactly these nine values, case-sensitive)

`NEW`, `ToContact`, `Contacted`, `TourBooked`, `Toured`, `Applied`,
`Rejected`, `Skipped`, `Secured`.

`backlog.validate_actions()` rejects any other value with an error naming
the offending unit id and the allowed list. Nothing coerces an unknown
value to a nearby-looking valid one - a typo fails loudly instead of
silently losing the user's intent.

### Ownership rule (read this before writing any code that touches this file)

- **The user owns every field of every existing entry.** Tooling never
  rewrites, reorders, or deletes a `status`, `note`, or `updated_at` that
  is already present. Hand-editing this file - flipping a status, adding a
  note - is the intended interface, not a workaround.
- **The only automated mutation is appending a `status: NEW` entry for a
  unit id that exists in `verified.json` but has no entry here yet.**
  `backlog.sync_new_units(actions, verified_units, now)` does exactly this
  and nothing else: it returns a new dict equal to its input plus one
  freshly-appended record per newly-verified unit, `updated_at` set to
  `now`. It never touches a key that was already present.
- **The on-disk write is a text append, never a parse-and-rewrite.**
  `backlog.append_new_entries(path, before, after)` renders only the
  newly-synced entries and appends them as text to the end of the file.
  It never parses the whole file into a dict and dumps it back out, so a
  hand-set status, a hand-chosen entry order, and any comments already in
  the file survive a scan or hydrate run byte-for-byte - the only change
  on disk is new text appended at the end. When there is nothing new to
  append, the file is not opened for writing at all.
- A unit id absent from this file entirely is treated as `NEW` by every
  downstream reader (`backlog.build_backlog`, and eventually the dashboard
  action panel) - so a missing file behaves identically to a file where
  every unit is explicitly `NEW`.
- `updated_at` is a plain date (`YYYY-MM-DD`), not a timestamp, because a
  human edits this file directly and a date is what a human writes.

### Join key

`unit_id` must match the id used to key `verified.json` and
`data/backlog-state.json`. `backlog.unit_key(unit)` derives this id: it
prefers an explicit `unit_id` field on the verified-unit record if one
exists (future scan/verify tickets may add one), and otherwise falls back
to a slug of `building` + `unit`, e.g. `"Example Tower 2"` unit `"2207"`
becomes `example-tower-2-2207`.

Note this fallback slug style does NOT match the example keys shown in
`verify_units.py`'s docstring (e.g. `"tower2-2207"`) - `verify_units.py`
does not derive a key itself, its `"key"` field is caller-supplied input
in the checks file the caller builds, so there is nothing there to match
against. Whoever wires `verified.json` writing should adopt an explicit
`unit_id` field eventually so this fallback derivation stops being
load-bearing; until then, `unit_key()` is the single source of truth for
the id and every consumer must call it rather than re-deriving one.

## apartmentops/data/backlog-state.json  (SYSTEM-OWNED, auto-written)

```json
{
  "example-tower-2-2207": {"resurfaced": 2, "last_run_id": "hyd-20260708-090000"},
  "example-tower-2-2311": {"resurfaced": 0, "last_run_id": "hyd-20260701-090000"}
}
```

- `resurfaced`: how many times this unit has actually appeared in a
  rendered Backlog section (not how many hydrate runs have happened -
  units that were eligible but ranked below the top-N cap do not burn a
  strike; see `backlog.build_backlog`'s docstring for the exact rule).
- `last_run_id`: the run_id of the most recent hydrate run that evaluated
  this unit for backlog purposes (whether or not it was actually shown).
  Used to detect "this unit's actions.yml entry predates the current run"
  so a unit first verified this run is news, not backlog, on its first
  pass.
- Once `resurfaced` reaches the configured `max_resurface` (default 3),
  the unit is permanently demoted to the Stale backlog list and is never
  promoted back while its status stays `NEW`. If the user later changes
  the unit's status away from `NEW`, its entry is deleted from this file
  entirely (decay resets), so if the status is ever changed back to `NEW`
  by hand, resurfacing starts over from zero rather than picking up where
  a long-stale counter left off.
- This file is entirely disposable: deleting it just restarts every unit's
  resurface count at zero. It carries no user intent and needs no backup.

## Why the split

`actions.yml` states user intent and must never be touched by anything
except the one documented append. `backlog-state.json` states pipeline
bookkeeping (a nag counter) that must reset and evolve on its own schedule,
independent of whatever the user has written in their notes. Folding the
counter into `actions.yml` would force every automated backlog run to
rewrite a file the user is simultaneously hand-editing, and the first
skipped merge would either clobber a note or silently stop decaying -
exactly the class of bug this split avoids by construction.

## Draft outreach text

`backlog.build_backlog()` attaches a `draft_outreach_note` string to every
backlog and unscored row, generated by
`backlog._draft_outreach_note(unit_id, unit)` from fields already present
on the verified-unit record (building, unit number, address, price) - it
never invents a fact that is not on the record. This text is for the user
to copy, edit, and send themselves. No code path in this codebase sends it
anywhere; it is display text only, surfaced in the hydrate run report and,
later, the dashboard action panel.
