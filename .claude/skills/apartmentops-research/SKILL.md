---
name: apartmentops-research
description: >-
  ApartmentOps stage 3 - grade the neighborhoods and price the commute. For
  every area that appears in apartmentops/data/verified.json, researches
  safety (crime statistics), cleanliness (sanitation and rodent-complaint
  data, resident sentiment), nearby essentials with walk times, and computes
  door-to-door transit time and monthly fare cost to the user's anchor.
  Writes apartmentops/data/areas.json and transit.json. Use when the user
  asks "how safe is this area", "what is the commute really like", "how
  clean are these neighborhoods", or after a scan produces units in areas
  that have not been graded yet.
---

# ApartmentOps: Research (safety, cleanliness, transit)

Inputs: `apartmentops/config.yml`, `apartmentops/data/verified.json`.
Outputs: `apartmentops/data/areas.json`, `apartmentops/data/transit.json`.
Schemas in `../apartmentops/references/contracts.md`. Only research areas
that actually contain live units - do not grade the whole city.

## Area grading (one researcher per area, parallel)

Each area researcher must return, with a URL cited for every claim:

1. **Safety** - violent and property crime vs the city and national
   averages, from published crime-statistics sources (police precinct or
   department data, established crime-grading sites, local news). Produce a
   letter grade (A-F, plus/minus allowed) with a one-line justification
   containing actual numbers. Capture block-level nuance when it exists: the
   doormanned tower block and the strip two streets over can grade
   differently, and saying so is more useful than an average.
2. **Cleanliness** - sanitation scorecards and rodent/litter complaint data
   where the city publishes them (e.g. NYC 311 and rat-map data), resident
   sentiment otherwise. Letter grade + justification. Note structural
   factors (flood-prone blocks, construction dust) honestly.
3. **Essentials** - the 4-8 nearest groceries, pharmacies, parks, gyms, and
   transit entrances with realistic walk minutes.
4. **Vibe** - one or two honest sentences, including the risks the listing
   sites will not mention.

Rules: grades must trace to sources; if data is thin, grade what you can
and say what you could not. If an area was not researched, omit it entirely -
the dashboard shows "n/a", and an honest n/a beats an invented B.

## Transit (one analyst)

For the anchor in config (skip if null): compute weekday peak door-to-door
transit from each area to the anchor as walk-to-station + typical wait +
ride + final walk. Two traps to call out explicitly:

- Use REAL station walk times from the map, not marketing copy.
- Fare systems differ: check current fares (they change yearly), whether
  transfers between systems are free (often they are not - stacked fares
  can double a monthly cost), and compute the monthly cost at ~42
  rides/month using the cheapest sensible option (pass vs pay-per-ride).

Write transit.json with per-area typical minutes, range, route description,
and monthly cost, plus the fare table with source URLs.

## Wrap up

Summarize the grades in a compact table, flag any area whose grade should
change how the user weighs its units (a bargain in a C area is a different
product than the same price in an A area), and offer the next stage:
`apartmentops-dashboard`.
