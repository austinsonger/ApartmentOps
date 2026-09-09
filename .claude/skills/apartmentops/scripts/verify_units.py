#!/usr/bin/env python3
"""Headless liveness checker for apartment listings.

Feed it a JSON file of checks; it loads each URL in headless Chromium,
classifies the page it actually got (unit page, table, index, wall, shell),
reports a three-state verdict per unit, reads the price only where the
surface makes it admissible, harvests per-unit deep links, and screenshots
everything.

Input file format (JSON array):
    [{"key": "tower2-2207", "token": "2207",
      "url": "https://example.com/availability",
      "shot": "apartmentops/shots/tower2-2207.png",
      "prior": "live",
      "policy": {"kind": "table", "complete": true}}]

`prior` (optional) is the verdict already on file for the unit ("live",
"gone", or "check"). `policy` (optional) is a per-check source-policy
entry that overrides the `--policy` file for that one URL.

Usage:
    python3 verify_units.py checks.json [--policy apartmentops/sources.yml] > results.json

Output: one row per check. Every field the previous version wrote is still
written; the new fields are additive.
    key, checked_at, live, price_near, unit_links, error, screenshot
    verdict      "live" | "check" | "gone"
    why          one line of evidence for the verdict
    kind         own_page | table | index | plan_page | aggregator | gated |
                 untrusted | unknown
    final_url    where the browser actually landed (a redirect reveals a wall)
    body_chars   length of the rendered body text
    body_hash    sha1 of the normalized body, for the shared-shell rule
    token_found  whether the unit token appeared on the page
    price        integer monthly price when admissible, else null
    price_layer  "net" | "base" | "total" | "unknown" | null
    price_trust  "ok" | "none"
    review       true when the page contradicts a prior "gone" on file

`live` is derived from `verdict`: True = live, False = gone, None = check.
A null `live` means "keep the prior verdict and its date". It is never a
gone. `price_near` is kept for compatibility but is now populated only
when `price_trust` is "ok" (the unit's own page or its own table row).

Verdict rules (learned one weekly pass at a time):
- An index or root page confirms live (token present) but never proves
  gone (token absent). Indexes paginate and lazy-load.
- A login wall, bot wall, CAPTCHA interstitial, empty shell, or fetch
  error keeps the prior verdict. None of these can overturn a prior gone.
- A complete, unpaginated operator table that omits the unit IS evidence
  of gone. `complete: true` comes from the source policy; a pagination
  marker in the body ("load more", "show more", ...) cancels it.
- Some operators render ANY invented unit id with a price on their
  per-unit deep link. The source policy declares those deep links
  `untrusted` and the operator's index authoritative instead; an untrusted
  surface is never evidence either way.
- Gone requires a unit-specific page (own_page, or a complete table) whose
  body rendered fully (>= SHELL_MIN_CHARS), is not a shell shared with
  other units, and omits the token.
- Identical page bodies across SHELL_SHARED_MIN or more units of one host
  are a shell, not a set of gone units: every such gone is demoted to
  check after the batch.
- A price near a unit token on an index page is unusable. Prices are read
  only from the unit's own page or from the unit's own table row, and the
  price layer is detected (net-effective asterisk, base rent, total
  monthly) so a net figure is never stored as gross.
- Label rows such as "2BR-3" carry no unit number and can never be
  verified gone.
- A page that shows the token for a unit whose prior verdict is "gone"
  does not flip it back to live by itself: the row comes back as check
  with `review: true` and `token_found: true`, for a human to re-check.
- Search for the token with boundaries that tolerate zero-padding
  ("0407" vs "407").
- Without a policy, a URL that names the unit token is treated as the
  unit's own page and an index-looking path as an index. An operator
  whose per-unit URL renders any invented id will then read as a false
  LIVE, not a false gone; that is exactly what the `untrusted` policy kind
  exists to prevent, so write the policy.

Source policy (`--policy FILE`, YAML or JSON, a `sources:` list; the shape
is documented in references/contracts.md): each entry is
    {match, kind, live_evidence, gone_evidence, price_evidence,
     complete, price_layer, note}
matched by substring against the URL. FIRST MATCH WINS: list the more
specific pattern (a per-unit path) before the generic one (its index).

Requires: pip install playwright && playwright install chromium
(pyyaml only when the policy file is YAML).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import re
import sys
from collections import defaultdict
from typing import Any
from urllib.parse import urlsplit

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)

VERDICT_LIVE = "live"
VERDICT_CHECK = "check"
VERDICT_GONE = "gone"

# A body shorter than this did not render: it can never prove gone.
SHELL_MIN_CHARS = 350
# Identical bodies across this many units of one host are one shell page.
SHELL_SHARED_MIN = 3
# Body wall markers only count on a short body, and a bare "login" is not
# one of them: a listing page with a "Resident Login" link in its
# navigation is not a wall. The URL is the stronger signal (a redirect to
# /login or /signin) and is always checked.
WALL_BODY_MAX_CHARS = 2500

WALL_URL_MARKERS = ("login", "signin", "captcha", "cloudflare", "guestlogin", "userlogin")
WALL_BODY_MARKERS = (
    "captcha", "cloudflare", "just a moment", "access denied",
    "verify you are human", "guestlogin", "userlogin",
    "log in to continue", "sign in to continue", "please log in", "please sign in",
)
INDEX_PATH_MARKERS = (
    "/availabilit", "/floorplans", "/floor-plans", "/apartments", "/residences",
    "/rentals", "availability.aspx", "/listings", "/homes", "/units",
)
PAGINATION_MARKERS = (
    "load more", "show more", "view more", "see more", "next page", "page 2",
    "load all", "more results", "show all",
)
LABEL_TOKEN_RE = re.compile(r"^\s*(?:\d\s?(?:BR|BD|BED)|STUDIO)\b", re.IGNORECASE)
PRICE_RE = re.compile(r"\$\s?(\d{1,2},\d{3}|\d{4,5})(?!\d)")

UNIT_SPECIFIC_KINDS = ("own_page", "table")
INDEX_KINDS = ("index", "plan_page", "aggregator")


def token_pattern(token: str) -> re.Pattern:
    # Tolerate zero-padding on either side: "407" matches "0407" and "407".
    core = token.lstrip("0") or token
    return re.compile(
        r"(?<![0-9A-Za-z])0*" + re.escape(core) + r"(?![0-9])", re.IGNORECASE
    )


def is_label_token(token: str) -> bool:
    """A bed-count label ("2BR-3", "Studio") or a token with no digit is a
    row label, not a unit number. It can be found, never verified gone."""
    t = (token or "").strip()
    return not t or not re.search(r"\d", t) or bool(LABEL_TOKEN_RE.match(t))


def is_root_url(url: str) -> bool:
    try:
        return urlsplit(url).path.strip("/") == ""
    except ValueError:
        return False


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def body_hash(body: str) -> str:
    """sha1 of the body with whitespace collapsed and every digit run
    replaced, so a shell that echoes a request id, a timestamp, or the
    unit number it was asked for still hashes the same across units."""
    normalized = re.sub(r"\s+", " ", body or "").strip().lower()
    normalized = re.sub(r"\d+", "#", normalized)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def load_sources(path: str | pathlib.Path) -> list[dict]:
    """Load a `sources:` policy list from YAML or JSON. Missing pyyaml is a
    loud stop, never a silent fall back to heuristics."""
    p = pathlib.Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yml", ".yaml"):
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            raise SystemExit(
                "policy file is YAML but pyyaml is not installed: pip install pyyaml"
            ) from None
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("sources") or []
    if not isinstance(data, list):
        raise ValueError("policy must be a list of source entries or {sources: [...]}")
    return [dict(entry) for entry in data if isinstance(entry, dict)]


def policy_for(url: str, sources: list[dict] | None) -> dict:
    """First entry whose `match` is a substring of the URL wins, so a
    per-unit path must be listed before its index pattern."""
    for entry in sources or []:
        match = entry.get("match")
        if match and match in (url or ""):
            return entry
    return {}


def infer_kind(url: str, final_url: str, token: str) -> str:
    """Policy-less fallback: a URL naming the unit token is its own page;
    an index-looking path or a site root is an index; anything else is
    unknown and can never prove gone."""
    if token and not is_label_token(token) and token.lower() in (url or "").lower():
        return "own_page"
    paths = []
    for u in (url, final_url):
        try:
            paths.append((urlsplit(u or "").path or "").lower())
        except ValueError:
            paths.append("")
    if is_root_url(url or "") or is_root_url(final_url or ""):
        return "index"
    if any(m in p for p in paths for m in INDEX_PATH_MARKERS):
        return "index"
    return "unknown"


def detect_wall(final_url: str, body: str, kind: str | None) -> str | None:
    """Return a reason string when the page is a login, bot, or CAPTCHA
    wall, else None."""
    low = (final_url or "").lower()
    if kind == "gated":
        return "gated surface (session wall by policy)"
    hit = next((m for m in WALL_URL_MARKERS if m in low), None)
    if hit:
        return "wall in final URL (%s)" % hit
    if kind is None and "securecafe" in low:
        return "securecafe host with no policy entry; treated as gated"
    if len(body or "") < WALL_BODY_MAX_CHARS:
        bl = (body or "").lower()
        hit = next((m for m in WALL_BODY_MARKERS if m in bl), None)
        if hit:
            return "wall marker in short body (%s, %d chars)" % (hit, len(body or ""))
    return None


def price_layer(text: str, policy: dict | None = None) -> str:
    """Which price layer a snippet shows: net-effective (asterisk or the
    words), base rent, total monthly, else the policy's declared layer,
    else unknown."""
    t = text or ""
    if re.search(r"\$\s?[\d,.]+\s?\*", t) or re.search(r"net[\s-]*effective", t, re.I):
        return "net"
    if re.search(r"base rent", t, re.I):
        return "base"
    if re.search(r"total monthly|total rent", t, re.I):
        return "total"
    declared = (policy or {}).get("price_layer")
    return str(declared) if declared else "unknown"


def _own_row(body: str, match: re.Match) -> str:
    """The text line holding the token plus the next few lines: the unit's
    own table row on an operator table."""
    lines = body.splitlines()
    pos = 0
    for i, line in enumerate(lines):
        end = pos + len(line) + 1
        if pos <= match.start() < end:
            return " | ".join(l.strip() for l in lines[i:i + 5] if l.strip())
        pos = end
    return body[match.start(): match.start() + 300]


def price_for(kind: str | None, body: str, match: re.Match, policy: dict) -> tuple[int | None, str | None, str]:
    """(price, layer, trust). Admissible only on the unit's own page or in
    the unit's own table row, and only when the policy allows prices."""
    if kind not in UNIT_SPECIFIC_KINDS or policy.get("price_evidence", True) is False:
        return None, None, "none"
    if kind == "table":
        snippet = _own_row(body, match)
        m = PRICE_RE.search(snippet)
    else:
        # The unit's own page: only a price near the token counts. A price
        # elsewhere on the page can be a deposit, a fee, or a concession
        # figure, so there is no page-wide fallback.
        snippet = body[max(0, match.start() - 150): match.start() + 250]
        m = PRICE_RE.search(snippet)
    if not m:
        return None, None, "none"
    return int(m.group(1).replace(",", "")), price_layer(snippet, policy), "ok"


def classify(url: str, final_url: str, body: str, token: str, policy: dict | None = None,
             prior: str | None = None) -> dict[str, Any]:
    """Pure verdict for one rendered page. No browser, no network."""
    policy = dict(policy or {})
    body = body or ""
    kind = policy.get("kind") or None
    inferred = kind is None
    if inferred:
        kind = infer_kind(url, final_url, token)
    out: dict[str, Any] = {
        "verdict": VERDICT_CHECK, "why": "", "kind": kind, "token_found": False,
        "price": None, "price_layer": None, "price_trust": "none",
        "body_chars": len(body), "body_hash": body_hash(body), "review": False,
    }
    if kind == "untrusted":
        out["why"] = "untrusted surface by policy: %s" % (policy.get("note") or "renders any unit id")[:80]
        return out
    if is_label_token(token):
        out["why"] = "label row without a unit number; nothing to verify"
        return out

    match = token_pattern(token).search(body)
    out["token_found"] = bool(match)
    wall = detect_wall(final_url, body, None if inferred else kind)

    if match and policy.get("live_evidence", True) is not False:
        price, layer, trust = price_for(kind, body, match, policy)
        out.update({"price": price, "price_layer": layer, "price_trust": trust})
        if prior == VERDICT_GONE:
            out["review"] = True
            out["why"] = "page shows the token but the unit is gone on file; review by hand"
            return out
        out["verdict"] = VERDICT_LIVE
        out["why"] = "token on %s" % kind
        if kind in INDEX_KINDS or kind == "unknown":
            out["why"] += " (index-class page: live only, price not admissible)"
        return out
    if match:
        out["why"] = "token present but surface carries no live evidence by policy"
        return out

    if wall:
        out["why"] = wall + "; prior verdict kept"
        return out
    if kind in INDEX_KINDS:
        out["why"] = "%s page cannot prove gone (paginates or lazy-loads); prior verdict kept" % kind
        return out
    if kind in ("gated", "unknown"):
        out["why"] = "%s surface cannot prove gone; prior verdict kept" % kind
        return out
    if len(body) < SHELL_MIN_CHARS:
        out["why"] = "empty shell (%d chars); prior verdict kept" % len(body)
        return out
    if kind == "table":
        bl = body.lower()
        pager = next((m for m in PAGINATION_MARKERS if m in bl), None)
        if pager:
            out["why"] = "table shows a pagination marker (%s); not complete, prior verdict kept" % pager
            return out
        if not policy.get("complete"):
            out["why"] = "table not declared complete by policy; absence is not proof"
            return out
    if policy.get("gone_evidence", True) is False:
        out["why"] = "unit absent but surface lacks gone evidence by policy"
        return out
    out["verdict"] = VERDICT_GONE
    out["why"] = "%s rendered fully (%d chars) and omits the unit" % (
        "complete table" if kind == "table" else "own page", len(body))
    return out


def apply_shell_rule(rows: list[dict], shared_min: int = SHELL_SHARED_MIN) -> list[dict]:
    """Demote every gone whose body is byte-identical to that of
    `shared_min` or more other units on the same host: that is one shell
    page, not several gone units. Mutates and returns rows."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("verdict") == VERDICT_GONE and r.get("body_hash"):
            groups[(host_of(r.get("final_url") or ""), r["body_hash"])].append(r)
    for members in groups.values():
        if len(members) >= shared_min:
            for r in members:
                r["verdict"] = VERDICT_CHECK
                r["live"] = None
                r["why"] = "identical shell shared by %d units on one host; prior verdict kept" % len(members)
    return rows


def live_from_verdict(verdict: str) -> bool | None:
    return {VERDICT_LIVE: True, VERDICT_GONE: False}.get(verdict)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="verify_units.py",
        description="Three-state (live / check / gone) headless liveness checker.",
    )
    ap.add_argument("checks", help="JSON array of {key, token, url, shot?, prior?, policy?}")
    ap.add_argument("--policy", default=None,
                    help="sources policy file (YAML or JSON) with a sources: list; first match wins")
    args = ap.parse_args(argv)

    checks = json.loads(pathlib.Path(args.checks).read_text())
    sources = load_sources(args.policy) if args.policy else []

    from playwright.sync_api import sync_playwright

    results: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 1200})
        for check in checks:
            page = ctx.new_page()
            row: dict[str, Any] = {
                "key": check["key"],
                "checked_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "live": None,
                "price_near": None,
                "unit_links": [],
                "error": None,
                "verdict": VERDICT_CHECK,
                "why": "",
                "kind": None,
                "final_url": None,
                "body_chars": 0,
                "body_hash": None,
                "token_found": False,
                "price": None,
                "price_layer": None,
                "price_trust": "none",
                "review": False,
            }
            policy = check.get("policy") or policy_for(check["url"], sources)
            try:
                page.goto(check["url"], timeout=45000, wait_until="domcontentloaded")
                page.wait_for_timeout(5000)
                body = re.sub(r"[ \t]+", " ", page.inner_text("body"))
                row["final_url"] = page.url
                verdict = classify(check["url"], page.url, body, check["token"], policy, check.get("prior"))
                row.update(verdict)
                row["live"] = live_from_verdict(row["verdict"])
                if row["price_trust"] == "ok" and row["price"] is not None:
                    row["price_near"] = "$" + format(row["price"], ",")
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
            except Exception as exc:  # fail-soft per unit: a fetch error is a check, never a gone
                row["error"] = f"{type(exc).__name__}: {str(exc)[:140]}"
                row["verdict"] = VERDICT_CHECK
                row["live"] = None
                row["why"] = "fetch error; prior verdict kept"
                row["kind"] = policy.get("kind") or row["kind"]
            results.append(row)
            page.close()
        browser.close()

    apply_shell_rule(results)
    json.dump(results, sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
