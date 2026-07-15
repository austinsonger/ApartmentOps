#!/usr/bin/env python3
"""Geocode an address via OSM Nominatim (polite: 1 request/second).

Usage:
    python3 geocode.py "20 W 34th St, New York, NY"
    python3 geocode.py --json "some address"   # machine-readable output

Nominatim usage policy requires a descriptive User-Agent and max 1 req/sec.
For bulk geocoding, sleep between calls (this script handles one address).
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request

ENDPOINT = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "ApartmentOps/1.0 (apartment-search assistant)"


def geocode(address: str) -> dict | None:
    url = ENDPOINT + "?" + urllib.parse.urlencode(
        {"q": address, "format": "json", "limit": 1, "addressdetails": 0}
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        results = json.load(resp)
    if not results:
        return None
    top = results[0]
    return {
        "lat": round(float(top["lat"]), 6),
        "lon": round(float(top["lon"]), 6),
        "display_name": top.get("display_name", ""),
    }


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--json"]
    as_json = "--json" in sys.argv
    if not args:
        print("usage: geocode.py [--json] <address>", file=sys.stderr)
        return 2
    result = geocode(" ".join(args))
    if result is None:
        print("NOT FOUND" if not as_json else json.dumps(None))
        return 1
    if as_json:
        print(json.dumps(result))
    else:
        print(f"{result['lat']} {result['lon']}  | {result['display_name'][:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
