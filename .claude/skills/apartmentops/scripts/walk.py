#!/usr/bin/env python3
"""Walk Score lookup for one building address, with a file cache.

Reads the public Walk Score score page for an address and pulls the Walk
Score (and the Transit and Bike scores when the page shows them). No API
key, no login - a plain read-only GET of a public page.

Usage:
    python3 walk.py "1 Example Ave" Chicago IL
    python3 walk.py "1 Example Ave" Chicago IL --cache apartmentops/data/cache/walk.json

Library usage:
    from walk import walk_score
    result = walk_score("1 Example Ave", "Chicago", "IL", cache_path="walk.json")
    # {"score": 92, "transit": 80, "bike": 75, "source_url": "...", "fetched_at": "..."}

Contract (never guessed, never defaulted):
- On success: {score, transit, bike, source_url, fetched_at}. transit and
  bike are null when the page does not show them.
- On ANY failure (network error, block page, no parsable Walk Score on the
  page): {score: None, error: "<reason>", source_url, fetched_at}. The
  dashboard renders "walk: n/a"; nothing downstream fills the gap.
- Only successes are cached; a failure always retries on the next run. A
  cache entry older than cache_days is refetched. The cache is keyed by
  norm_address(address) plus city and state.

Page structure (a dated observation, not a guarantee, in the same spirit
as references/platforms.md): as of 2026-10-03 the score page URL shape is
https://www.walkscore.com/score/<street-city-state slug>, and the scores
have historically been rendered as badge images whose paths end in
/badge/walk/score/<n>.svg, /badge/transit/score/<n>.svg and
/badge/bike/score/<n>.svg, with alt text of the form "<n> Walk Score of
<address>". parse_score() reads the badge paths first and the alt text
second. This shape was not confirmed against a live page from the
environment that wrote it; if lookups start failing with "no walk score on
page", re-check a live page and update the patterns and this date.

Stdlib only. fetch(url) -> bytes is injectable (default: urllib, 20s
timeout) so tests run with zero network access.
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
import re
import sys
import urllib.request

from shortlist import norm_address  # one row-key rule for the cache and the shortlist

SCORE_ENDPOINT = "https://www.walkscore.com/score/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)
TIMEOUT_SECONDS = 20
DEFAULT_CACHE_DAYS = 90

_BADGE = {
    kind: re.compile(rf"/badge/{kind}/score/(\d{{1,3}})\.svg", re.IGNORECASE)
    for kind in ("walk", "transit", "bike")
}
_ALT = {
    "walk": re.compile(r"(\d{1,3})\s+Walk\s+Score\s+of", re.IGNORECASE),
    "transit": re.compile(r"(\d{1,3})\s+Transit\s+Score\s+of", re.IGNORECASE),
    "bike": re.compile(r"(\d{1,3})\s+Bike\s+Score\s+of", re.IGNORECASE),
}


def default_fetch(url: str) -> bytes:
    """Plain read-only GET, 20s timeout. Raises on any transport error."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return resp.read()


def _slug(text: str) -> str:
    text = re.sub(r"[^a-z0-9\s-]", "", (text or "").lower())
    return re.sub(r"[\s-]+", "-", text).strip("-")


def build_score_url(address: str, city: str, state: str) -> str:
    """https://www.walkscore.com/score/<street>-<city>-<state>, lowercased,
    spaces to hyphens, punctuation stripped."""
    parts = [_slug(p) for p in (address, city, state)]
    return SCORE_ENDPOINT + "-".join(p for p in parts if p)


def _score(html: str, kind: str) -> int | None:
    for pattern in (_BADGE[kind], _ALT[kind]):
        m = pattern.search(html)
        if m:
            value = int(m.group(1))
            if 0 <= value <= 100:
                return value
    return None


def parse_score(html: str | None) -> dict:
    """{"walk", "transit", "bike"} as ints or None. A page with no parsable
    Walk Score returns all None (transit and bike are not trusted alone)."""
    html = html or ""
    walk = _score(html, "walk")
    if walk is None:
        return {"walk": None, "transit": None, "bike": None}
    return {"walk": walk, "transit": _score(html, "transit"), "bike": _score(html, "bike")}


# Walk Score range -> walkability anchor band (examples/scoring.example.yml).
_WALK_BANDS = ((90, 100, 17, 20), (70, 89, 13, 16), (50, 69, 9, 12), (0, 49, 1, 8))


def walkability_raw(score: int | None) -> int | None:
    """Map a Walk Score (0-100) onto the walkability dimension's 1-20 raw
    scale, linearly inside the anchor band its range belongs to (90+ ->
    17-20, 70-89 -> 13-16, 50-69 -> 9-12, under 50 -> 1-8). None stays None
    so the dimension renormalizes instead of being guessed."""
    if score is None:
        return None
    score = max(0, min(100, int(score)))
    for lo, hi, raw_lo, raw_hi in _WALK_BANDS:
        if lo <= score <= hi:
            return raw_lo + round((score - lo) / (hi - lo) * (raw_hi - raw_lo))
    return None


def _as_datetime(now) -> _dt.datetime:
    if now is None:
        return _dt.datetime.now().astimezone()
    if isinstance(now, str):
        value = _dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
    else:
        value = now
    return value if value.tzinfo else value.astimezone()


def _cache_key(address: str, city: str, state: str) -> str:
    return f"{norm_address(address)}|{_slug(city)}|{_slug(state)}"


def _load_cache(cache_path) -> dict:
    path = pathlib.Path(cache_path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(cache_path, cache: dict) -> None:
    path = pathlib.Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def walk_score(address: str, city: str, state: str, *, cache_path=None,
               cache_days: int = DEFAULT_CACHE_DAYS, now=None, fetch=default_fetch) -> dict:
    """Walk Score for one address. See the module docstring for the shape.

    A cache hit fetched within cache_days of `now` returns the cached
    object with cached: true and makes no network call.
    """
    now_dt = _as_datetime(now)
    fetched_at = now_dt.isoformat(timespec="seconds")
    source_url = build_score_url(address, city, state)

    cache: dict = {}
    key = _cache_key(address, city, state)
    if cache_path:
        cache = _load_cache(cache_path)
        hit = cache.get(key)
        if isinstance(hit, dict) and hit.get("fetched_at"):
            try:
                age = now_dt - _as_datetime(hit["fetched_at"])
            except ValueError:
                age = None
            if age is not None and age <= _dt.timedelta(days=cache_days):
                return dict(hit, cached=True)

    def failure(error: str) -> dict:
        return {"score": None, "error": error, "source_url": source_url, "fetched_at": fetched_at}

    try:
        raw = fetch(source_url)
    except Exception as exc:  # noqa: BLE001 - any transport failure is n/a, never guessed
        return failure(f"fetch failed: {type(exc).__name__}: {exc}")
    html = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
    scores = parse_score(html)
    if scores["walk"] is None:
        return failure("no walk score on page (blocked, moved, or address not found)")

    result = {"score": scores["walk"], "transit": scores["transit"], "bike": scores["bike"],
              "source_url": source_url, "fetched_at": fetched_at}
    if cache_path:
        cache[key] = result
        _save_cache(cache_path, cache)
    return result


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cache_path = None
    if "--cache" in args:
        idx = args.index("--cache")
        if idx + 1 >= len(args):
            print('usage: walk.py "ADDRESS" CITY STATE [--cache PATH]', file=sys.stderr)
            return 2
        cache_path = args[idx + 1]
        del args[idx: idx + 2]
    if len(args) != 3:
        print('usage: walk.py "ADDRESS" CITY STATE [--cache PATH]', file=sys.stderr)
        return 2
    result = walk_score(args[0], args[1], args[2], cache_path=cache_path)
    print(json.dumps(result, indent=1))
    return 0 if result.get("score") is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
