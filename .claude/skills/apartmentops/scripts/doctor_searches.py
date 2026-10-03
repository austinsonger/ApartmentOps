#!/usr/bin/env python3
"""Preflight check for apartmentops/config.yml's saved_searches block.

Each saved search is a pre-built, fully filtered, newest-first listing URL
(price band, beds, sort baked in) that a scan subagent lands on directly
instead of driving a platform's search UI. Driving search UI per run is
brittle - filter widgets change - baking the query into the URL removes
that failure class. This script re-validates every URL each run so a scan
never silently trusts a saved search that quietly broke.

saved_searches shape (config.yml)::

    saved_searches:
      primary:
        platform_a:
          neighborhood_x: "https://...&beds=2&price_max=2600&sort=newest"
      fallback_1br:               # optional secondary profile
        platform_a:
          neighborhood_x: "https://...&beds=1&price_max=2100&sort=newest"

profile -> platform -> area -> URL. A leaf can also be the richer form
{"url": "...", "marker": "<regex>"} - see check_saved_searches() below;
the marker form is strongly preferred because it actually confirms a
nonzero result count instead of guessing from page size.

build_saved_searches(config, recipes) expands per-platform URL templates
(read by onboarding from references/platforms.md) into that same shape, so
the model never hand-expands a recipe. A platform with no template (Redfin:
numeric neighborhood ids that only the UI knows) gets a {"needs_ui": true}
leaf until the scan drives the UI once and caches the URL.

Guardrails: every fetch is a read-only GET to a public page, no logins, no
retries against a bot wall (a 403 is reported and left alone - never
bypassed).

CLI:
    python3 doctor_searches.py config.yml
    python3 doctor_searches.py saved_searches.json

Reading a .yml config requires PyYAML (pip install pyyaml); a .json file
needs nothing beyond the standard library.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Callable

DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)
# Only used when a saved search has no "marker" configured. The marker form
# is strongly preferred - it actually confirms a nonzero result count. A
# bare body-length threshold is a weak proxy (a big "no results" page can
# still be long) kept only so an un-markered search still reports something
# rather than nothing.
DEFAULT_BODY_LENGTH_THRESHOLD = 2000

FetchFn = Callable[[str], "tuple[int, str]"]


def default_fetch(url: str) -> tuple[int, str]:
    """Read-only GET with a desktop UA and a 15s timeout. Never retries."""
    request = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            status = response.getcode() or 200
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read().decode(charset, errors="replace")
            return status, body
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            if exc.fp is not None:
                body = exc.fp.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return exc.code, body
    except urllib.error.URLError as exc:
        return 0, f"URLError: {exc.reason}"
    except TimeoutError:
        return 0, "TimeoutError: request exceeded timeout"


def _leaf_url_and_marker(leaf: Any) -> tuple[str | None, str | None]:
    if isinstance(leaf, str):
        return leaf, None
    if isinstance(leaf, dict):
        return leaf.get("url"), leaf.get("marker")
    return None, None


SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
CITYWIDE = "citywide"


def _slug(value: Any, what: str) -> str:
    if not isinstance(value, str) or not SLUG_RE.match(value):
        raise ValueError(f"{what} {value!r} is not a valid slug (lowercase letters, digits, hyphens; no spaces)")
    return value


def _expand(template: str, values: dict[str, Any], platform: str) -> str:
    out = template
    for name in re.findall(r"\{(\w+)\}", template):
        if values.get(name) is None:
            raise ValueError(f"{platform}: template needs {{{name}}} but no value is configured")
        out = out.replace("{" + name + "}", str(values[name]))
    return out


def build_saved_searches(config: dict[str, Any], recipes: dict[str, Any]) -> dict[str, Any]:
    """Expand platform recipes into a saved_searches block.

    recipes: {platform: {"url": template | None, "marker": regex | None,
    "per_area": bool, "citywide_url": template (optional), "note": str
    (optional)}}. Templates use {city_slug}, {neighborhood_slug},
    {max_rent} and {craigslist_subdomain}.

    Values come from config["locale"]["platform_slugs"] (city_slug,
    craigslist_subdomain, area_slugs: {area: slug}), config["geography"]
    ["areas"] and config["budget"]["gross_max"]. A per_area recipe yields
    one leaf per area (neighborhood slug from area_slugs, else the area
    name); with no areas, or per_area false, one "citywide" leaf, using
    citywide_url when given. A recipe with no url yields {"needs_ui":
    true, "note": ...}. Every slug substituted is validated; a bad one
    raises ValueError.

    Returns {"primary": {platform: {area: {"url", "marker"?} |
    {"needs_ui", "note"}}}}, the shape check_saved_searches accepts.
    """
    config = config or {}
    slugs = ((config.get("locale") or {}).get("platform_slugs") or {})
    areas = list((config.get("geography") or {}).get("areas") or [])
    area_slugs = slugs.get("area_slugs") or {}
    gross_max = (config.get("budget") or {}).get("gross_max")
    base: dict[str, Any] = {
        "max_rent": int(gross_max) if gross_max is not None else None,
        "city_slug": _slug(slugs["city_slug"], "city_slug") if slugs.get("city_slug") is not None else None,
        "craigslist_subdomain": (_slug(slugs["craigslist_subdomain"], "craigslist_subdomain")
                                 if slugs.get("craigslist_subdomain") is not None else None),
    }

    primary: dict[str, Any] = {}
    for platform, recipe in (recipes or {}).items():
        recipe = recipe or {}
        template = recipe.get("url")
        marker = recipe.get("marker")
        if not template:
            keys = areas if (recipe.get("per_area") and areas) else [CITYWIDE]
            primary[platform] = {
                k: {"needs_ui": True,
                    "note": recipe.get("note") or "no URL template; drive the platform UI once and cache the URL"}
                for k in keys
            }
            continue
        leaves: dict[str, Any] = {}
        if recipe.get("per_area") and areas:
            for area in areas:
                values = dict(base, neighborhood_slug=_slug(area_slugs.get(area, area), f"slug for area {area!r}"))
                leaves[area] = _leaf(_expand(template, values, platform), marker)
        else:
            leaves[CITYWIDE] = _leaf(_expand(recipe.get("citywide_url") or template, base, platform), marker)
        primary[platform] = leaves
    return {"primary": primary}


def _leaf(url: str, marker: str | None) -> dict[str, Any]:
    return {"url": url, "marker": marker} if marker else {"url": url}


def check_saved_searches(
    saved_searches: dict[str, Any],
    fetch: FetchFn | None = None,
    body_length_threshold: int = DEFAULT_BODY_LENGTH_THRESHOLD,
) -> list[dict[str, Any]]:
    """Preflight every URL in saved_searches (profile -> platform -> area ->
    url or {url, marker}).

    A search PASSES when the fetch returns HTTP 200 AND either:
      - a "marker" regex is configured and matches at least once in the
        body (this confirms an actual nonzero result count - strongly
        preferred), or
      - no marker is configured and the body is longer than
        body_length_threshold (a weak fallback signal only).

    A 403 is never retried - it is reported as a bot wall and left alone.

    Returns report rows: [{profile, platform, area, url, ok, status,
    detail}, ...], one row per configured URL, in saved_searches order.
    """
    fetch = fetch or default_fetch
    rows: list[dict[str, Any]] = []

    for profile, platforms in (saved_searches or {}).items():
        for platform, areas in (platforms or {}).items():
            for area, leaf in (areas or {}).items():
                url, marker = _leaf_url_and_marker(leaf)
                row: dict[str, Any] = {
                    "profile": profile,
                    "platform": platform,
                    "area": area,
                    "url": url,
                    "ok": False,
                    "status": None,
                    "detail": "",
                }
                if not url and isinstance(leaf, dict) and leaf.get("needs_ui"):
                    row["detail"] = "needs_ui: drive the platform UI once, cache the URL, re-run preflight"
                    rows.append(row)
                    continue
                if not url:
                    row["detail"] = "no url configured for this profile/platform/area"
                    rows.append(row)
                    continue

                status, body = fetch(url)
                row["status"] = status

                if status == 403:
                    row["detail"] = "bot wall (HTTP 403) - not retried"
                    rows.append(row)
                    continue
                if status != 200:
                    row["detail"] = f"HTTP {status}"
                    rows.append(row)
                    continue

                if marker:
                    try:
                        matched = re.search(marker, body) is not None
                    except re.error as exc:
                        row["detail"] = f"invalid marker regex {marker!r}: {exc}"
                        rows.append(row)
                        continue
                    if matched:
                        row["ok"] = True
                        row["detail"] = f"marker {marker!r} matched"
                    else:
                        row["detail"] = f"marker {marker!r} did not match (likely zero results)"
                else:
                    length = len(body or "")
                    if length > body_length_threshold:
                        row["ok"] = True
                        row["detail"] = (
                            f"no marker configured (marker form is strongly preferred); "
                            f"body length {length} > threshold {body_length_threshold}"
                        )
                    else:
                        row["detail"] = (
                            f"no marker configured; body length {length} "
                            f"<= threshold {body_length_threshold}"
                        )

                rows.append(row)

    return rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_saved_searches(path: pathlib.Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yml", ".yaml"):
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:
            raise SystemExit(
                "PyYAML is required to read a .yml config. "
                "Install it with: pip install pyyaml"
            ) from exc
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text) if text.strip() else {}
    # Accept either a full config.yml (pull out saved_searches) or a file
    # that IS already just the saved_searches block.
    if isinstance(data, dict) and "saved_searches" in data:
        return data["saved_searches"] or {}
    return data


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: doctor_searches.py config.yml", file=sys.stderr)
        return 2

    path = pathlib.Path(argv[0])
    saved_searches = _load_saved_searches(path)
    rows = check_saved_searches(saved_searches)

    json.dump(rows, sys.stdout, indent=1)
    print()

    failed = [row for row in rows if not row["ok"]]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
