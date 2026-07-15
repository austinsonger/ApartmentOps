# Line-substitution advisor

Towers repeat floor plans in vertical lines: unit 1205 is typically the
same floor plan and orientation as unit 1805, six floors up.
This reference documents the curated line-map file format, how it gets
populated, and the shape of the advisor output that `line_advisor.py`
computes from it.
See `.claude/skills/apartmentops/scripts/line_advisor.py` for the
implementation; its module docstring and function docstrings are the
authoritative contract - this file is the narrative companion.

## Why a curated file, not inference

A trailing letter on a unit number ("1205C") looks like a line label, but
treating it as one without confirmation is a guess.
Some buildings do not use letter-suffixed lines at all, some reuse a
letter for unrelated units, and "same letter" does not always mean "same
floor plan or orientation."
The advisor never infers line identity from unit numbers alone, from
photos, or from floor-plan images.
It only trusts a line that a hand-curated map confirms exists, and it
only asserts orientation the map itself cites to a source.
A building with no curated map produces no advisor output at all -
silently, never with a guessed placeholder.

## File location and schema

One file per building: `apartmentops/data/lines/{building-slug}.yml`
(YAML; PyYAML is a guarded import, only required on the code path that
loads these files - `pip install PyYAML` if missing).

```yaml
building: "Example Tower 2"          # must exactly match verified.json's
                                      # "building" field for this building -
                                      # this is the join key, not the filename
source: "https://example.com/tower2/floor-plans"   # official floor plan or
                                                     # key plan URL - cited,
                                                     # never left blank
lines:
  C:
    orientation: S                   # cite-or-omit; never a guessed letter
    floors: "5-40"                   # free text; informational only today
  D:
    orientation: SE
    floors: "5-40"
units:                                # OPTIONAL explicit unit -> line
                                      # overrides, exact-string keyed
  "3201": "C"                        # e.g. a corner unit whose number
                                      # does not end in its line's letter
```

Field notes:

- `building` is the join key `line_advisor.build_lines_index()` uses to
  match this file to rows in `apartmentops/data/verified.json` (via
  `load_all_line_maps()`, which keys its returned dict by this field, not
  by the filename).
  The filename slug is for on-disk organization only.
  If `building` here does not exactly match verified.json's `building`
  string for every row of that building, those units silently get no
  advisor output (fail-soft, not a crash) - keep the two in sync.
- `source` should be an official floor plan or key plan URL.
  Anti-fabrication applies here as everywhere in ApartmentOps: if you
  cannot cite a source for a line's existence or orientation, do not add
  it to the map.
  Do not derive orientation from an aerial photo, an agent's claim, or a
  guess.
- `lines` is the set of confirmed lines.
  A unit's trailing letters (or an explicit override) are trusted as a
  line ONLY if that exact key appears here.
- `units` is optional and exact-string keyed against `verified.json`'s
  `unit` field (no normalization, e.g. no leading-zero stripping).
  Use it for units whose number does not carry a trailing line letter, or
  where the trailing letter would be misleading.

## Who populates this file

`apartmentops-scan` and `apartmentops-research` are the natural
producers: when either stage finds a building's official floor-plan or
key-plan page (the same kind of source `verify_units.py`-style browser
verification already visits), it can hand-write or update this file,
citing that URL in `source`.
This file lives under `apartmentops/data/lines/`, which is user-project
runtime data (gitignored, like the rest of `apartmentops/`) - it is
curated per search, not shipped as system-layer content.
Nothing in this repo auto-generates or auto-edits it from unit numbers; a
human (or an agent citing a real source) adds each line.

## The advisor's inputs and output

`line_advisor.py` exposes three library functions, importable directly
(no I/O, no network):

- `unit_line(unit_str, line_map) -> str | None` - resolves one unit
  string to a confirmed line name, or `None` if unconfirmed.
- `suggest(candidate, verified_units, line_map, trajectories) -> dict` -
  the per-candidate cross-join described below.
- `build_lines_index(verified_units, line_maps_by_building, trajectories) -> (index, report)` -
  the batch driver that runs `suggest()` for every live unit in a mapped
  building and assembles the full index.

`trajectories` is `{unit_id: [{price, status, observed_at}, ...]}`, the
per-unit price/status/availability series the Market memory epic's
snapshot-ledger trajectory extraction produces from
`apartmentops/data/snapshots.jsonl`.
`line_advisor.py` does not read `snapshots.jsonl` itself - it consumes
whatever trajectory dict is handed to it, keeping the two epics' file
-format ownership separate.
The CLI's `build` command expects this dict as a ready-made JSON file.

### `suggest()` output shape

```json
{
  "siblings": [
    {"unit_id": "tower2-1205c", "floor_delta": -6, "price_delta": -310,
     "same_line": true, "live": true}
  ],
  "line_history": {"times_listed": 5, "price_min": 2350, "price_max": 2600}
}
```

- `siblings`: one entry per OTHER unit in the same building resolved to
  the same line, live or historical.
  `floor_delta` and `price_delta` are signed `(sibling - candidate)`
  values - a sibling six floors down and $310/month cheaper reports
  `floor_delta: -6, price_delta: -310`.
  Either delta is `null` when the floor or price on either side is
  unknown - never fabricated.
  Sort order: live siblings cheaper than the candidate first (cheapest
  first), then everything else by price delta.
- `line_history`: `null` when fewer than 2 priced trajectory observations
  exist across the line within the window handed to `suggest()` -
  "insufficient ledger data" - rendered explicitly by the dashboard,
  never presented as zero history.
  Otherwise `times_listed` counts distinct listing stints (transitions
  into `status: "live"`, including a unit's first-ever observation)
  across every unit in the line, and `price_min`/`price_max` span every
  priced observation in that window.

### `build_lines_index()` output shape (this is `apartmentops/data/lines.json`)

The CLI (`python3 line_advisor.py build verified.json lines_dir trajectories.json`)
writes:

```json
{
  "lines": {
    "tower2-1205c": {"siblings": [...], "line_history": {...} },
    "tower2-1805c": {"siblings": [...], "line_history": null}
  },
  "report": {
    "live_candidates": 40,
    "no_line_map": 12,
    "unparsed_unit": 3,
    "included": 25
  }
}
```

`lines` is keyed by `unit_id` and contains an entry ONLY for live units
whose building has a curated map AND whose unit number resolves to a
confirmed line.
A building with no map, or a unit that fails to resolve, is simply
absent, never present with a placeholder.
`report` is the run report: `no_line_map` counts live units in unmapped
buildings, `unparsed_unit` counts live units in mapped buildings whose
number still did not resolve to a confirmed line (both are visible,
counted exclusions, never silent).

## How this feeds unit cards and dossiers (consumer contract)

This ticket produces the data/computation layer only; rendering is a
later integration phase.
What that phase needs:

- For a unit card, look up `lines.json["lines"][unit_id]`.
  If the key is absent, render nothing (no line map, or unresolved line -
  both silent).
  If present, render a collapsed "Line alternatives (N)" row where N is
  `len(siblings)`, expanding to one row per sibling with its floor delta,
  signed price delta, live/gone state, and its OWN deep link (resolve the
  sibling's `unit_id` against `verified.json`'s `unit_deep_link` field -
  `line_advisor.py` does not carry deep links itself, to avoid
  duplicating data already keyed by `unit_id` in verified.json).
- Below the siblings, render the `line_history` line.
  If `null`, render "history: n/a (insufficient ledger data)"; otherwise
  render something like "{line} line listed {times_listed} times in the
  last 12 months at ${price_min}-${price_max}", plus (for full
  traceability) the `observed_at` values behind it, which the rendering
  layer can recompute by re-filtering `trajectories` the same way
  `recent_observations()` did, or by having `build_lines_index` (or a
  small wrapper) additionally persist the contributing `observed_at`
  timestamps if the dashboard needs them inline without recomputation.
- Per-unit dossiers can surface the same `lines.json` entry as a "Line
  alternatives" section using the same field names.

## Guardrails (repeated from the epic, binding)

- Informational only: the advisor never contacts a broker or landlord and
  never auto-sends anything.
  A human acts on a suggestion via the sibling's own deep link.
- No guessed data: a building without a curated line map produces no
  output; insufficient trajectory history renders "n/a (insufficient
  ledger data)", never a fabricated range; a unit whose number cannot be
  confirmed against the map is skipped and counted, never guessed.
- Zero network requests: everything here is computation over
  files/dicts already in memory.
