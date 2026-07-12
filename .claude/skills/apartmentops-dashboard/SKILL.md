---
name: apartmentops-dashboard
description: >-
  ApartmentOps stage 4 - build or re-hydrate the interactive map dashboard.
  Renders every verified unit onto a to-scale SVG map with commute lines to
  the user's anchor (time + monthly cost per leg), safety and cleanliness
  grade chips that link to their sources, per-unit listing deep links, and
  live/gone badges - then publishes it as an artifact with a stable URL.
  "Hydrate" mode re-checks every unit against its own deep link and updates
  prices and gone badges without re-hunting. Use when the user says "build
  the dashboard", "update my dashboard", "hydrate", "is everything still
  live", or after scan + research have produced their data files.
---

# ApartmentOps: Dashboard (build + hydrate)

Inputs: `apartmentops/config.yml`, `data/verified.json`, `data/areas.json`,
`data/transit.json`. Output: `apartmentops/dashboard.html`, published as an
artifact (republish the SAME file path every time so the URL never changes).
Study `../apartmentops/assets/example-dashboard.html` before building - it
is a working instance of every pattern below.

## Build

One self-contained HTML file (inline CSS/JS, no external hosts). The pieces
that make it trustworthy rather than decorative:

- **Real geography, to scale.** Project lat/lon linearly onto the SVG
  viewBox and draw actual shoreline/boundary polygons (city open-data
  boundary files clipped to shoreline work; simplify to a few hundred points
  per ring). Legal county/municipal boundaries extend INTO rivers and will
  flood your water - use shoreline-clipped sources. Hand-drawn coastlines
  read as wonky; users notice.
- **Commute lines drawn honestly**: separate walk legs (dotted) from transit
  legs (solid, colored per mode) through real station coordinates, labeled
  with door-to-door minutes and monthly cost from transit.json.
- **Markers = safety grade.** Color each building marker by its area's
  safety grade letter; markers for ungraded areas get a neutral "?".
- **Cards per building**: commute line + route text, a unit table (floor,
  rent with net/gross both shown, sqft, availability, a LISTING link per
  unit using the deepest known URL, and a live/gone badge with the check
  date), essentials chips with walk minutes, and the honest vibe sentence
  including negatives.
- **Every grade chip is a link** to its primary source. No source, no grade -
  render "n/a" instead.
- **Both themes.** Define tokens on :root, override in
  prefers-color-scheme:dark AND :root[data-theme="dark"]/[data-theme="light"].
  Validate chart/marker colors against both surfaces before shipping.
- Sort controls (commute / cheapest / safety), hover tooltips, marker-card
  cross-highlighting. Render it headless and LOOK at the screenshot before
  publishing - label collisions and geometry bugs hide from validators.

## Hydrate (re-verification without re-hunting)

When the user asks to refresh: re-check every unit against its own
`unit_deep_link` (never an availability index - indexes paginate and produce
false "gone" verdicts), update `rent_verified`/`live`/`verified_at` in
verified.json, mark gone rows with a dated badge (keep them - they are
market history and show the user how fast the market moves), update the
dashboard's freshness stamp and KPI counts, republish to the same URL, and
report the diff: what leased, what repriced, what direction prices moved.

At typical bands, units disappear within days - if the user has a target
move-in date, offer a scheduled hydrate cadence rather than manual refreshes.
