#!/usr/bin/env python3
"""Keyless FEMA flood-zone lookup for a single lat/lon point.

Queries the public FEMA National Flood Hazard Layer (NFHL) ArcGIS REST
MapServer, layer 28, with a point geometry. No API key, no login - it is a
plain HTTP GET against a federal public-data service.

Usage:
    python3 flood.py 40.7357 -74.0270
    python3 flood.py 40.7357 -74.0270 --cache apartmentops/data/cache/flood.json

Library usage:
    from flood import flood_zone
    result = flood_zone(40.7357, -74.0270, cache_path="flood-cache.json")
    # {"zone": "AE", "subtype": None, "sfha": True,
    #  "source_url": "...", "viewer_url": "...", "fetched_at": "..."}

Contract (never guessed, never defaulted to "no risk"):
- On success: {zone, subtype, sfha (bool|null), source_url, viewer_url,
  fetched_at}. zone/subtype come straight from the service; sfha is True/
  False when SFHA_TF parses cleanly, else null - a parse miss on that one
  field does not fail the whole lookup.
- On ANY failure (missing coordinates, network error, non-JSON response,
  empty result set, a feature with no FLD_ZONE) the whole lookup fails:
  {zone: None, error: "<reason>", source_url, viewer_url, fetched_at}.
  An "empty result" (the service found no mapped flood polygon at this
  point) is reported as this kind of failure too, per the design decision
  in the ticket: unmapped is n/a, never "no risk".

Only genuinely successful lookups (a resolved zone) are written to the
cache. Failures are never cached, so a transient network error or a not-
yet-mapped point gets retried on the next run instead of being locked in.

Requires only the standard library. fetch(url) -> bytes is an injectable
parameter (default: urllib, 20s timeout) so tests and callers can supply a
fake transport with zero network access.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

QUERY_ENDPOINT = (
    "https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer/28/query"
)
VIEWER_ENDPOINT = "https://msc.fema.gov/portal/search"
USER_AGENT = "ApartmentOps/1.0 (apartment-search assistant; flood-zone lookup)"
TIMEOUT_SECONDS = 20

_SFHA_TRUE = {"T", "TRUE", "1", "Y", "YES"}
_SFHA_FALSE = {"F", "FALSE", "0", "N", "NO"}


def default_fetch(url: str) -> bytes:
    """Plain GET via urllib, 20s timeout, descriptive User-Agent.

    Raises whatever urllib raises (URLError, HTTPError, timeout) - callers
    of flood_zone() do not need to catch this themselves, it is caught
    internally and turned into the standard failure shape.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return resp.read()


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _cache_key(lat: float, lon: float) -> str:
    return f"{round(lat, 4)},{round(lon, 4)}"


def _load_cache(cache_path: str) -> dict:
    path = pathlib.Path(cache_path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(cache_path: str, cache: dict) -> None:
    path = pathlib.Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n")


def _build_query_url(lat: float, lon: float) -> str:
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF",
        "returnGeometry": "false",
        "f": "json",
    }
    return QUERY_ENDPOINT + "?" + urllib.parse.urlencode(params, safe=",")


def _build_viewer_url(lat: float, lon: float) -> str:
    # Best-effort deep link into FEMA's public Map Service Center search box,
    # which accepts a "lat,lon" search term. This opens the FEMA flood map
    # viewer scoped to the coordinate; it is a convenience chip link, not a
    # claim about how the third-party page renders once loaded.
    query = urllib.parse.quote(f"{lat},{lon}")
    return f"{VIEWER_ENDPOINT}?AddressQuery={query}#searchresultsanchor"


def _parse_sfha(value) -> bool | None:
    if value is None:
        return None
    token = str(value).strip().upper()
    if token in _SFHA_TRUE:
        return True
    if token in _SFHA_FALSE:
        return False
    return None


def _failure(source_url, viewer_url, error: str, fetched_at: str) -> dict:
    return {
        "zone": None,
        "error": error,
        "source_url": source_url,
        "viewer_url": viewer_url,
        "fetched_at": fetched_at,
    }


def flood_zone(
    lat: float | None,
    lon: float | None,
    fetch=None,
    cache_path: str | None = None,
) -> dict:
    """Look up the FEMA NFHL flood zone at (lat, lon).

    fetch: injectable fetch(url) -> bytes, default `default_fetch` (real
    network). Pass a fake in tests - no network calls happen in test code.
    cache_path: optional plain JSON file, keyed by "lat,lon" rounded to 4
    decimals. A cache hit skips the network entirely and returns the
    originally-fetched result unchanged (including its original
    fetched_at). Only successes are cached; failures always retry.
    """
    fetch = fetch or default_fetch
    fetched_at = _now_iso()

    if lat is None or lon is None:
        return _failure(None, None, "missing coordinates", fetched_at)

    cache: dict = {}
    key = _cache_key(lat, lon)
    if cache_path:
        cache = _load_cache(cache_path)
        if key in cache:
            return cache[key]

    query_url = _build_query_url(lat, lon)
    viewer_url = _build_viewer_url(lat, lon)

    try:
        raw = fetch(query_url)
    except Exception as exc:  # noqa: BLE001 - any transport failure is n/a, never guessed
        return _failure(
            query_url, viewer_url, f"fetch failed: {type(exc).__name__}: {exc}", fetched_at
        )

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        return _failure(
            query_url, viewer_url, f"unparseable response: {type(exc).__name__}: {exc}", fetched_at
        )

    if not isinstance(data, dict):
        return _failure(query_url, viewer_url, "response was not a JSON object", fetched_at)

    if "error" in data:
        return _failure(query_url, viewer_url, f"service error: {data['error']}", fetched_at)

    features = data.get("features") or []
    if not features:
        return _failure(
            query_url,
            viewer_url,
            "empty result: no NFHL flood zone data at this point",
            fetched_at,
        )

    attrs = features[0].get("attributes") or {}
    zone = attrs.get("FLD_ZONE")
    if not zone:
        return _failure(query_url, viewer_url, "response missing FLD_ZONE", fetched_at)

    result = {
        "zone": zone,
        "subtype": attrs.get("ZONE_SUBTY") or None,
        "sfha": _parse_sfha(attrs.get("SFHA_TF")),
        "source_url": query_url,
        "viewer_url": viewer_url,
        "fetched_at": fetched_at,
    }

    if cache_path:
        cache[key] = result
        _save_cache(cache_path, cache)

    return result


def main() -> int:
    args = sys.argv[1:]
    cache_path = None
    if "--cache" in args:
        idx = args.index("--cache")
        if idx + 1 >= len(args):
            print("usage: flood.py LAT LON [--cache PATH]", file=sys.stderr)
            return 2
        cache_path = args[idx + 1]
        del args[idx : idx + 2]

    if len(args) != 2:
        print("usage: flood.py LAT LON [--cache PATH]", file=sys.stderr)
        return 2

    try:
        lat = float(args[0])
        lon = float(args[1])
    except ValueError:
        print("LAT and LON must be numbers", file=sys.stderr)
        return 2

    result = flood_zone(lat, lon, cache_path=cache_path)
    print(json.dumps(result, indent=1))
    return 0 if result.get("zone") is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
