#!/usr/bin/env python3
"""Extract listing fields from a saved/rendered listing page's embedded JSON.

Operates on HTML *text* only - it never opens a browser or a network
connection. Feed it the HTML a headless-browser verify pass already fetched
(page.content() in Playwright, or a saved .html file) and it looks for
machine-readable payloads before anything falls back to brittle DOM
scraping:

  1. script#__NEXT_DATA__          -> kind "next_data"
  2. script[type=application/ld+json] -> kind "ld_json" (one entry per tag)
  3. inline `window.NAME = {...}`  -> kind "window_state"

Each discovered payload becomes {"kind": ..., "data": <parsed JSON>}. A page
with none of the above yields an empty list - that is a valid, recorded
outcome ("payload: none" in the extractor spec), never an error.

extract_fields(payloads, spec) then resolves a small set of dot/bracket
paths against those payloads. A path that cannot be resolved against any
discovered payload comes back as MISSING (value None, method None) - it is
never guessed and never DOM-scraped by this module. DOM fallback, if a
platform needs it, is the verify pass's job (see references/provenance.md);
this module only ever tags what it found as "embedded:<kind>".

extractor spec shape (parsed from apartmentops/extractors/{platform}.yml,
see examples/extractor.example.yml):

    platform: some_platform
    payload: next_data            # which kind to prefer; optional
    fields:                       # field name -> dot/bracket path
      price: props.pageProps.listing.price
      beds: props.pageProps.listing.beds
    # Historical extractor.yml files (per the probe ticket) spelled the
    # field-name -> path mapping "paths:" instead of "fields:". Both keys
    # are accepted; "fields:" wins if both are present.

Path syntax:
    a.b.c        nested dict access
    a[0].b       list index (also accepts a.0.b)
    a.*.b        wildcard search - try every item of the list/dict at this
                 step and return the first one where the rest of the path
                 resolves; also spelled a[*].b

CLI:
    python3 extract_embedded.py page.html spec.yml > fields.json

Reading spec.yml requires PyYAML (pip install pyyaml); a plain .json spec
file needs nothing beyond the standard library. The library functions
(find_embedded_payloads, extract_fields, resolve_path) never need YAML -
tests and callers can hand them an already-parsed dict.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Payload discovery
# ---------------------------------------------------------------------------

_NEXT_DATA_RE = re.compile(
    r'<script[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_LD_JSON_RE = re.compile(
    r'<script[^>]*\btype=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
# Any inline (no src=) <script> block, for the window-state scan. A script
# tag that also matches one of the two patterns above is scanned too, but
# its body is straight JSON (no "window." prefix) so it never double-fires.
_INLINE_SCRIPT_RE = re.compile(
    r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_WINDOW_ASSIGN_RE = re.compile(
    r'window\.([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*'
)

KIND_NEXT_DATA = "next_data"
KIND_LD_JSON = "ld_json"
KIND_WINDOW_STATE = "window_state"


def _try_json(text: str) -> Any:
    """Parse text as JSON; return None (never raise) if it does not parse."""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _extract_json_value_at(text: str, start: int) -> Any:
    """Parse one JSON value starting at index `start`, skipping whitespace.

    Uses json's own decoder (raw_decode) rather than bracket counting, so
    braces/brackets inside quoted strings are handled correctly. Returns
    None if no valid JSON value starts there.
    """
    idx = start
    length = len(text)
    while idx < length and text[idx] in " \t\r\n":
        idx += 1
    if idx >= length:
        return None
    decoder = json.JSONDecoder()
    try:
        value, _end = decoder.raw_decode(text, idx)
    except json.JSONDecodeError:
        return None
    return value


def find_embedded_payloads(html: str) -> list[dict[str, Any]]:
    """Find every machine-readable JSON payload embedded in an HTML page.

    Returns a list of {"kind": "next_data"|"ld_json"|"window_state",
    "data": <parsed JSON>}, in discovery order. A page with none yields [].
    Unparseable script bodies are silently skipped (never guessed at).
    """
    payloads: list[dict[str, Any]] = []

    for match in _NEXT_DATA_RE.finditer(html):
        parsed = _try_json(match.group(1))
        if parsed is not None:
            payloads.append({"kind": KIND_NEXT_DATA, "data": parsed})

    for match in _LD_JSON_RE.finditer(html):
        parsed = _try_json(match.group(1))
        if parsed is not None:
            payloads.append({"kind": KIND_LD_JSON, "data": parsed})

    for script_match in _INLINE_SCRIPT_RE.finditer(html):
        body = script_match.group(1)
        for assign_match in _WINDOW_ASSIGN_RE.finditer(body):
            var_name = assign_match.group(1)
            value = _extract_json_value_at(body, assign_match.end())
            if value is not None:
                payloads.append(
                    {"kind": KIND_WINDOW_STATE, "data": value, "var": var_name}
                )

    return payloads


# ---------------------------------------------------------------------------
# Dot-path resolver
# ---------------------------------------------------------------------------

_MISSING = object()
_SEGMENT_RE = re.compile(r'^([^\[\]]*)((?:\[[^\]]*\])*)$')
_BRACKET_RE = re.compile(r'\[([^\]]*)\]')


def _tokenize_path(path: str) -> list[tuple[str, Any]]:
    """Split "a.b[0].*.c" into ('key','a') ('key','b') ('index',0)
    ('wildcard',None) ('key','c'). Raises ValueError on a malformed path."""
    tokens: list[tuple[str, Any]] = []
    for segment in path.split("."):
        if segment == "":
            raise ValueError(f"empty path segment in {path!r}")
        if segment == "*":
            tokens.append(("wildcard", None))
            continue
        match = _SEGMENT_RE.match(segment)
        if not match:
            raise ValueError(f"unparseable path segment {segment!r} in {path!r}")
        name, brackets = match.group(1), match.group(2)
        if name:
            if name.isdigit():
                tokens.append(("index", int(name)))
            else:
                tokens.append(("key", name))
        for bracket in _BRACKET_RE.findall(brackets):
            if bracket == "*":
                tokens.append(("wildcard", None))
            elif bracket.lstrip("-").isdigit():
                tokens.append(("index", int(bracket)))
            else:
                raise ValueError(f"unparseable bracket index {bracket!r} in {path!r}")
    return tokens


def _resolve_tokens(data: Any, tokens: list[tuple[str, Any]]) -> Any:
    if not tokens:
        return data
    kind, arg = tokens[0]
    rest = tokens[1:]
    if kind == "key":
        if isinstance(data, dict) and arg in data:
            return _resolve_tokens(data[arg], rest)
        return _MISSING
    if kind == "index":
        if isinstance(data, list) and -len(data) <= arg < len(data):
            return _resolve_tokens(data[arg], rest)
        return _MISSING
    if kind == "wildcard":
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            candidates = list(data.values())
        else:
            return _MISSING
        for item in candidates:
            result = _resolve_tokens(item, rest)
            if result is not _MISSING:
                return result
        return _MISSING
    return _MISSING  # pragma: no cover - exhaustive token kinds above


def resolve_path(data: Any, path: str) -> tuple[bool, Any]:
    """Resolve a dot/bracket path against parsed JSON data.

    Returns (found, value). found is False (value None) if any step of the
    path is missing, out of range, or the wrong shape - this never raises
    for a missing path, only for a malformed path string.
    """
    tokens = _tokenize_path(path)
    result = _resolve_tokens(data, tokens)
    if result is _MISSING:
        return False, None
    return True, result


# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------

def extract_fields(payloads: list[dict[str, Any]], spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Resolve spec's field paths against the discovered payloads.

    spec is a parsed extractor spec: {platform, payload: <preferred kind>,
    fields: {field_name: "dot.path"}} ("paths:" is accepted as an alias for
    "fields:" for compatibility with apartmentops/extractors/{platform}.yml
    files written by the probe routine).

    Returns {field_name: {"value": ..., "method": "embedded:<kind>"|None,
    "path": "<configured path>"}}. A field whose path cannot be resolved
    against any payload comes back with value None and method None - never
    guessed, never carried forward from elsewhere.
    """
    field_map: dict[str, str] = dict(spec.get("fields") or spec.get("paths") or {})
    preferred_kind = spec.get("payload")

    # Try the preferred kind's payloads first (if any exist), then fall back
    # to every other discovered payload in discovery order. This way a
    # stale "payload:" hint in the spec degrades gracefully instead of
    # blocking extraction outright.
    ordered_payloads = [p for p in payloads if p.get("kind") == preferred_kind]
    ordered_payloads += [p for p in payloads if p.get("kind") != preferred_kind]

    result: dict[str, dict[str, Any]] = {}
    for field_name, path in field_map.items():
        value = None
        method = None
        try:
            tokens_ok = True
            _tokenize_path(path)  # validate early so a bad path never raises later
        except ValueError:
            tokens_ok = False
        if tokens_ok:
            for payload in ordered_payloads:
                found, resolved = resolve_path(payload["data"], path)
                if found:
                    value = resolved
                    method = f"embedded:{payload['kind']}"
                    break
        result[field_name] = {"value": value, "method": method, "path": path}
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_spec(path: pathlib.Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yml", ".yaml"):
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:
            raise SystemExit(
                "PyYAML is required to read a .yml extractor spec. "
                "Install it with: pip install pyyaml"
            ) from exc
        return yaml.safe_load(text) or {}
    return json.loads(text) if text.strip() else {}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: extract_embedded.py page.html spec.yml", file=sys.stderr)
        return 2
    html_path, spec_path = pathlib.Path(argv[0]), pathlib.Path(argv[1])
    html_text = html_path.read_text(encoding="utf-8", errors="replace")
    spec = _load_spec(spec_path)
    payloads = find_embedded_payloads(html_text)
    fields = extract_fields(payloads, spec)
    json.dump(fields, sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
