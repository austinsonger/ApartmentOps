<!-- logo -->
<pre>
          _____________
         /            /|
        /____________/ |
        |  _   _   _ | |          A P A R T M E N T O P S
        | |_| |_| |_|| |          =========================
        |  _   _   _ | |          find it. verify it. map it.
        | |_| |_| |_|| |
        |  _   _   _ | |          a verified apartment-hunting
        | |_| |_| |_|| |          pipeline for Claude Code
        |  _   _   _ | |
        | |_| |_| |_|| |
        |  _   _   _ | |
        | |_| |_| |_|| /
        |  _   _   _ |/
     ___|_|_|_|_|_|_|___
    /__________________/|
    |__________________|/
</pre>

# ApartmentOps

A set of [Claude Code](https://claude.com/claude-code) skills that turn
"find me an apartment" into a verified, mapped, evidence-backed shortlist -
and let anyone set it up for their own city and commute from scratch.

Most apartment searches drown in stale listings, marketing walk-times, and
"net effective" prices that quietly revert at renewal. ApartmentOps is built
around one rule: **nothing counts unless it was verified against a live page
today, with the source cited.** No invented rents, no invented safety
grades, no auto-contacting brokers.

## How it works

Four skills that hand off through plain files in an `apartmentops/`
directory. Each stage reads the previous stage's output and writes its own,
so you can re-run any single stage without redoing the others.

```
  apartmentops             ->  apartmentops/config.yml
  (onboard from scratch)

  apartmentops-scan        ->  data/verified.json  (+ screenshot evidence)
  (hunt + browser-verify)

  apartmentops-research    ->  data/areas.json, data/transit.json
  (safety, cleanliness, commute cost)

  apartmentops-dashboard   ->  dashboard.html  (published, re-hydratable)
  (interactive map)
```

- **Onboard.** Asks where you commute to (down to the corner - a Financial
  District office and a Flatiron office produce completely different winning
  neighborhoods), your budget as a gross band, and which requirements are
  hard gates vs scoring bonuses. Geocodes the anchor and writes `config.yml`.
- **Scan.** Fans out parallel researchers per neighborhood, then confirms
  each candidate is live *today* in a real headless browser - screenshot
  evidence, per-unit deep links, scam screening, zombie-listing rejection.
- **Research.** Grades each area's safety and cleanliness from cited crime
  and sanitation data, lists nearby essentials with walk times, and computes
  real door-to-door transit time and monthly fare cost to your office.
- **Dashboard.** Renders everything on a to-scale map with real shorelines,
  commute lines, source-linked grade chips, per-unit listing links, and
  live/gone badges. "Hydrate" re-checks every unit later without re-hunting.

## Quickstart

1. Copy the four skill folders into your project's skills directory:

   ```bash
   git clone https://github.com/ajokunu/ApartmentOps
   cp -R ApartmentOps/.claude/skills/apartmentops* your-project/.claude/skills/
   ```

2. Install the browser used for verification (once):

   ```bash
   pip install playwright && playwright install chromium
   ```

3. Open your project in Claude Code and say:

   > set me up to find a 2 bedroom near my office

   The `apartmentops` skill triggers, walks you through onboarding, and
   offers to run the first scan. From then on: "rescan", "how safe is
   this area", "build the dashboard", "hydrate".

## What it looks like

Same pipeline, two different cities, light and dark:

| | Light | Dark |
|---|---|---|
| **Chicago** (Loop office) | ![Chicago light](docs/screenshots/chicago-light.png) | ![Chicago dark](docs/screenshots/chicago-dark.png) |
| **Boston** (Back Bay office) | ![Boston light](docs/screenshots/boston-light.png) | ![Boston dark](docs/screenshots/boston-dark.png) |

## Design principles

These are enforced in every skill and repeated to every subagent, because
subagents cut corners when the rules are implicit:

- **Anti-fabrication.** Every fact traces to a URL that was actually fetched
  or a file that was actually read. UNKNOWN is always an acceptable answer.
- **Liveness or it does not count.** A listing must be verifiably live on the
  day of the check. Years-old syndicated listings look identical to real ones
  until you read the page's own dates.
- **Per-unit deep links beat index pages.** Availability indexes paginate and
  lazy-load, producing false "gone" verdicts; verify against the unit's own
  page and keep that link.
- **Human-in-the-loop.** ApartmentOps never submits applications, books
  tours, or contacts a broker or leasing office. It produces drafts and
  links; you send them. Read-only browsing of public pages only - no logins,
  no bot-wall bypasses.

## Requirements

- Claude Code (or the Claude Agent SDK).
- Python 3.10+ with `playwright` and a Chromium install, for verification.
- No API keys required for the core pipeline. Geocoding uses the free OSM
  Nominatim service.

## Layout

```
.claude/skills/
  apartmentops/            onboarding + routing; references/, scripts/, assets/
    references/contracts.md  the file formats every stage reads and writes
    scripts/geocode.py       Nominatim geocoder
    scripts/verify_units.py  headless liveness checker
    assets/example-dashboard.html  a finished dashboard to study
  apartmentops-scan/       hunt + browser-verify
  apartmentops-research/   safety, cleanliness, transit
  apartmentops-dashboard/  build + hydrate the map
examples/config.example.yml
docs/screenshots/
```

## License

MIT. See [LICENSE](LICENSE). Built with Claude Code.
