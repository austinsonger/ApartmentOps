# Platform recipes: saved-search URLs, markers, walls, and quirks

Per-platform notes for building `saved_searches` during onboarding. These
are historical observations to verify against a live page before relying on
them, never guarantees: URL shapes, filter parameters, and result-count
text drift without notice, and a recipe that worked last month can silently
drop every filter today. `check_saved_searches` preflight is what proves a
URL works this run; this file only says where to start.

Onboarding reads the template code blocks below, builds a `recipes` dict
(`{platform: {"url": template | None, "marker": regex | None, "per_area":
bool}}`), and calls `scripts/doctor_searches.py`'s
`build_saved_searches(config, recipes)`, which substitutes the placeholders
and validates the slugs. Placeholders:

- `{city_slug}` - the platform's city slug, from `locale.platform_slugs.city_slug`
  (for example `chicago-il`).
- `{neighborhood_slug}` - per area, from `locale.platform_slugs.area_slugs`
  (falls back to the area name when no slug is set).
- `{max_rent}` - `budget.gross_max`, as an integer.
- `{craigslist_subdomain}` - from `locale.platform_slugs.craigslist_subdomain`
  (for example `chicago`).

Slugs are lowercase letters, digits, and hyphens only - never spaces.
These six recipes are US-only; a non-US `locale.country` skips them.

Pacing for every platform below: 3 to 5 seconds between page loads, and at
most two attempts per failed page. A bot wall is a stop signal, never a
puzzle: record it with `checked.add_blocker` and move on.

## Craigslist

### Search URL recipe

```
https://{craigslist_subdomain}.craigslist.org/search/apa?max_price={max_rent}&sort=date
```

City-wide (`per_area: false`); filter areas inline from each post's map
pin. `apa` is the apartments / housing for rent category, which carries
many owner ads.

### Marker regex

none observed

### Known walls

None observed; plain fetches have historically been served.

### Quirks

Neighborhood filters are numeric `nh` codes that differ per city; the
city-wide feed plus an inline geography filter avoids them. Post pages
(`/apa/d/...`) are each unit's own page and carry the asking price.

### Pacing

3 to 5 s between loads, two attempts per failed page.

### Observed on

2026-10-03: recipe written from the documented URL shape; not yet confirmed
against a live page (the environment that wrote it could not reach the
host). Confirm with preflight on first use and update this date.

## Apartments.com FRBO

### Search URL recipe

```
https://www.apartments.com/{neighborhood_slug}-{city_slug}/for-rent-by-owner/
```

City-wide form: `https://www.apartments.com/{city_slug}/for-rent-by-owner/`.

### Marker regex

none observed

### Known walls

Rate-limits plain fetches; expect a 403 or an empty shell from a
non-browser client. Use the headless browser, paced.

### Quirks

Ignores a price placed in the path, so the recipe carries no `{max_rent}`;
apply the band inline with `quality.feed_price_decision`. FRBO detail
pages are each unit's own page.

### Pacing

3 to 5 s between loads, two attempts per failed page.

### Observed on

2026-10-03: recipe written from the documented URL shape; not yet confirmed
against a live page. Confirm with preflight on first use and update this date.

## HotPads by-owner

### Search URL recipe

```
https://hotpads.com/{city_slug}/apartments-for-rent?price=0-{max_rent}&listingTypes=by-owner
```

City-wide (`per_area: false`).

### Marker regex

none observed

### Known walls

PerimeterX bot manager. A press-and-hold or CAPTCHA page is a wall: stop
and report, never solve.

### Quirks

The by-owner path or parameter may be ignored; when results include
managed buildings, use the UI's "Listed by: Property owner" filter once and
cache the resulting URL. A URL is only treated as a by-owner feed when it
still contains `by-owner`.

### Pacing

3 to 5 s between loads, two attempts per failed page.

### Observed on

2026-10-03: recipe written from the documented URL shape; not yet confirmed
against a live page. Confirm with preflight on first use and update this date.

## Zumper

### Search URL recipe

```
https://www.zumper.com/apartments-for-rent/{city_slug}?max-price={max_rent}
```

City-wide (`per_area: false`).

### Marker regex

none observed

### Known walls

None observed beyond ordinary rate limiting.

### Quirks

Neighborhoods are chosen through the UI selector, not a stable path slug;
drive the selector once per area and cache the URL if a per-area feed is
needed.

### Pacing

3 to 5 s between loads, two attempts per failed page.

### Observed on

2026-10-03: recipe written from the documented URL shape; not yet confirmed
against a live page. Confirm with preflight on first use and update this date.

## Zillow

### Search URL recipe

```
https://www.zillow.com/{neighborhood_slug}-{city_slug}/rentals/?price-max={max_rent}
```

City-wide form: `https://www.zillow.com/{city_slug}/rentals/?price-max={max_rent}`.

### Marker regex

none observed

### Known walls

PerimeterX bot manager; plain fetches are routinely blocked. Expect
preflight to report a wall, which is a reported result, not a failure to
work around.

### Quirks

Filters persist in an encoded `searchQueryState` parameter; a short URL can
redirect and drop the price filter. When the price filter is dropped, use
the URL the UI produces after setting the filter once.

### Pacing

3 to 5 s between loads, two attempts per failed page.

### Observed on

2026-10-03: recipe written from the documented URL shape; not yet confirmed
against a live page. Confirm with preflight on first use and update this date.

## Redfin

### Search URL recipe

No template. Redfin neighborhood and city IDs are numeric and must come
from the UI. `build_saved_searches` writes a `needs_ui: true` leaf; the
scan drives the UI once per area, caches the URL it produces into
`saved_searches`, and re-runs preflight on it.

### Marker regex

none observed

### Known walls

None observed beyond ordinary rate limiting.

### Quirks

Numeric IDs are per neighborhood and are not derivable from a name.

### Pacing

3 to 5 s between loads, two attempts per failed page.

### Observed on

2026-10-03: noted from the documented URL shape; not yet confirmed against
a live page.
