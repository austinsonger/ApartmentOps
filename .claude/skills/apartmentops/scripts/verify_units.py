#!/usr/bin/env python3
"""Headless liveness checker for apartment listings.

Feed it a JSON file of checks; it loads each URL in headless Chromium,
reports whether the unit token appears on the page, grabs the nearest
price, harvests per-unit deep links, and screenshots everything.

Input file format (JSON array):
    [{"key": "tower2-3410", "token": "3410",
      "url": "https://example.com/availability",
      "shot": "apartmentops/shots/tower2-3410.png"}]

Usage:
    python3 verify_units.py checks.json > results.json

Requires: pip install playwright && playwright install chromium

Notes learned the hard way:
- Search for the token with boundaries that tolerate zero-padding
  ("0503" vs "503").
- A token found on an index page is weak evidence; a token found on the
  unit's own page is strong. Prefer per-unit URLs in your checks.
- The nearest-price heuristic can grab an adjacent unit's price on dense
  index pages; treat price as approximate unless the page is unit-specific.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re
import sys

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)


def token_pattern(token: str) -> re.Pattern:
    # Tolerate zero-padding on either side: "503" matches "0503" and "503".
    core = token.lstrip("0") or token
    return re.compile(
        r"(?<![0-9A-Za-z])0*" + re.escape(core) + r"(?![0-9])", re.IGNORECASE
    )


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: verify_units.py checks.json", file=sys.stderr)
        return 2
    checks = json.loads(pathlib.Path(sys.argv[1]).read_text())

    from playwright.sync_api import sync_playwright

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 1200})
        for check in checks:
            page = ctx.new_page()
            row = {
                "key": check["key"],
                "checked_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "live": None,
                "price_near": None,
                "unit_links": [],
                "error": None,
            }
            try:
                page.goto(check["url"], timeout=45000, wait_until="domcontentloaded")
                page.wait_for_timeout(5000)
                body = page.inner_text("body")
                pat = token_pattern(check["token"])
                match = pat.search(body)
                row["live"] = bool(match)
                if match:
                    segment = body[max(0, match.start() - 150): match.start() + 250]
                    price = re.search(r"\$[\d,]{4,8}", segment)
                    if price:
                        row["price_near"] = price.group(0)
                hrefs = page.eval_on_selector_all(
                    "a[href]", "els => els.map(e => e.href)"
                )
                row["unit_links"] = sorted(
                    {h for h in hrefs if check["token"].lower() in h.lower()},
                    key=len,
                )[:5]
                shot = check.get("shot")
                if shot:
                    pathlib.Path(shot).parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=shot, full_page=False)
                    row["screenshot"] = shot
            except Exception as exc:  # fail-soft per unit
                row["error"] = f"{type(exc).__name__}: {str(exc)[:140]}"
            results.append(row)
            page.close()
        browser.close()

    json.dump(results, sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
