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
