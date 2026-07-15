#!/usr/bin/env python3
"""Perceptual-hash scam net over listing photos the pipeline already archived.

Every scam screen in the competitive scour is text-signal based (price
outliers, suspicious phrasing, contact-info patterns). This works at the
image level instead: two average-hash and difference-hash fingerprints per
photo, compared pairwise across the whole archive. A close match between
photos attached to different unit_ids is either a legitimate reused amenity
photo (excluded when the address also matches and the price does not) or
one of three scam patterns: a listing recycling another listing's photos
under a different address, the same address advertised at two wildly
different prices, or a photo that reappears on a unit previously recorded
as gone (a "zombie repost").

This module performs local computation only: it hashes files already on
disk and compares hex strings. It never fetches a URL, opens a browser, or
contacts a site. See references/line-substitution.md's sibling reference,
contracts.md, for how apartmentops/data/photo_hashes.json and the resulting
flags feed into verified.json entries and the dashboard's "Photo match"
badge.

Requires: pip install Pillow
(Pillow is imported lazily; every entry point raises a clear install hint
if it is missing rather than failing with a bare ImportError.)

Manifest input format (JSON array), one row per archived photo:
    [{"unit_id": "tower2-1205c", "address": "1 Example Ave, City, ST",
      "platform": "streeteasy", "price": 4900, "live": true,
      "photo_path": "apartmentops/shots/tower2-1205c-1.png"}]

CLI usage:
    python3 photo_hash.py index manifest.json > index.json
    python3 photo_hash.py scan index.json [max_distance] > matches.json

Notes learned the hard way:
- A hash is only ever computed from a real, readable image file. A row
  whose photo cannot be opened gets ahash/dhash set to null and an "error"
  field explaining why - it is never skipped silently and never assigned a
  fabricated hash.
- The distance threshold is empirical, not a law of nature. The default
  (5, out of 64 bits for the default 8x8 hash) is conservative for
  near-duplicate detection; tune it against real fixtures per building
  before trusting it market-wide, and treat near-misses (distance just
  above threshold) as worth a second look, not a proof of innocence.
- Same-address matches are expected and NOT inherently suspicious - a
  building's own amenity/lobby photos legitimately appear on every unit's
  listing. Those pairs are excluded unless the prices also diverge sharply,
  which turns "shared amenity photo" into "same address, different price"
  (a real scam pattern: bait-and-switch pricing off a real address).
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re
import sys

try:
    from PIL import Image
except ImportError:  # pragma: no cover - exercised only when Pillow is absent
    Image = None  # type: ignore[assignment]

_PILLOW_HINT = (
    "Pillow is required for photo_hash.py. Install it with: pip install Pillow"
)

DEFAULT_HASH_SIZE = 8
DEFAULT_MAX_DISTANCE = 5
PRICE_GAP_PCT_THRESHOLD = 25.0


def _require_pillow() -> None:
    if Image is None:
        raise ImportError(_PILLOW_HINT)


def _bits_to_hex(bits: str) -> str:
    width = -(-len(bits) // 4)  # ceil(len(bits) / 4)
    return f"{int(bits, 2):0{width}x}"


def average_hash(img: "Image.Image", size: int = DEFAULT_HASH_SIZE) -> str:
    """Average hash (aHash): 1 bit per pixel of an 8x8 (default) grayscale
    thumbnail, set if the pixel is at or above the thumbnail's mean.

    Accepts any PIL Image; converts to grayscale internally so callers can
    pass the image as opened.
    """
    _require_pillow()
    small = img.convert("L").resize((size, size), Image.LANCZOS)
    pixels = list(small.getdata())
    avg = sum(pixels) / len(pixels)
    bits = "".join("1" if p >= avg else "0" for p in pixels)
    return _bits_to_hex(bits)


def dhash(img: "Image.Image", size: int = DEFAULT_HASH_SIZE) -> str:
    """Difference hash (dHash): 1 bit per horizontal gradient across an
    (size+1) x size (default 9x8) grayscale thumbnail, set if a pixel is
    darker than its right-hand neighbor. More robust to gamma/brightness
    shifts than aHash, which is why find_matches() compares dHash values.

    Accepts any PIL Image; converts to grayscale internally.
    """
    _require_pillow()
    small = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    pixels = list(small.getdata())
    bits_chars = []
    for row in range(size):
        row_start = row * (size + 1)
        for col in range(size):
            left = pixels[row_start + col]
            right = pixels[row_start + col + 1]
            bits_chars.append("1" if left < right else "0")
    return _bits_to_hex("".join(bits_chars))


def hamming(a: str, b: str) -> int:
    """Hamming distance between two equal-length hex hash strings."""
    if len(a) != len(b):
        raise ValueError(f"hash length mismatch: {len(a)!r} vs {len(b)!r}")
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def hash_file(path: str) -> dict:
    """Open an image file, convert to grayscale, and return both hashes.

    Raises on any failure to open/read the file (missing file, corrupt
    image, unsupported format) - callers (build_index) are responsible for
    catching this and recording an explicit error rather than a fabricated
    hash.
    """
    _require_pillow()
    with Image.open(path) as raw:
        gray = raw.convert("L")
        return {
            "ahash": average_hash(gray),
            "dhash": dhash(gray),
        }


def build_index(manifest: list[dict]) -> dict:
    """Hash every photo in a manifest, preserving all provenance fields.

    manifest rows: {unit_id, address, platform, price, live, photo_path}
    (extra fields are preserved untouched). Returns:
        {"generated_at": "<iso8601>", "photos": [<row + ahash/dhash/error>]}

    A row whose photo cannot be read gets ahash=null, dhash=null, and a
    non-null "error" string - it is never skipped outright (its provenance
    stays visible) and never assigned a fabricated hash.
    """
    photos = []
    for row in manifest:
        entry = dict(row)
        photo_path = row.get("photo_path")
        try:
            if not photo_path:
                raise ValueError("manifest row has no photo_path")
            hashes = hash_file(photo_path)
            entry["ahash"] = hashes["ahash"]
            entry["dhash"] = hashes["dhash"]
            entry["error"] = None
        except Exception as exc:  # fail-soft per photo, never per whole index
            entry["ahash"] = None
            entry["dhash"] = None
            entry["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        photos.append(entry)
    return {
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "photos": photos,
    }


def normalize_address(address: str | None) -> str:
    """Loose address normalization for equality comparison only (case,
    punctuation, and whitespace insensitive). Not a geocoder - two
    differently-worded addresses for the same building will NOT be
    treated as equal by this function; that is a feature, not a bug, for
    a conservative scam net (false negatives here just mean a legitimate
    same-building pair gets a cross_address flag it doesn't deserve, which
    a human reviewing the evidence panel can dismiss; false positives would
    hide a real cross-address photo reuse)."""
    if not address:
        return ""
    lowered = address.strip().lower()
    return re.sub(r"[^a-z0-9]+", " ", lowered).strip()


def _pct_diff(a: float, b: float) -> float:
    lo = min(a, b)
    if lo == 0:
        return float("inf")
    return abs(a - b) / lo * 100.0


def find_matches(index: dict, max_distance: int = DEFAULT_MAX_DISTANCE) -> list[dict]:
    """Compare dHash across every pair of photos belonging to DIFFERENT
    unit_ids, at or below max_distance Hamming distance.

    Returns a list of:
        {"a_unit": unit_id, "b_unit": unit_id, "distance": int,
         "flags": [...]}

    flags (any subset, in this order when present):
        cross_address  - the two photos' normalized addresses differ
        cross_platform - the two photos' platform strings differ
        price_gap      - abs percent price difference exceeds 25%
        zombie_repost  - either side's "live" field is explicitly false

    Same-address, same-price pairs (shared amenity/lobby photos within one
    building) are expected and excluded entirely - not returned as a match
    at all - unless a price_gap or zombie_repost signal is also present,
    either of which turns "same address" into a real signal: a price_gap
    means bait-and-switch pricing off one real address, and zombie_repost
    means the same photo resurfaced on a unit previously recorded gone
    (which commonly happens at the SAME address and price as the original
    listing, so it must not be swallowed by the shared-amenity exclusion).
    """
    photos = [p for p in index.get("photos", []) if p.get("dhash")]
    matches: list[dict] = []
    for i in range(len(photos)):
        a = photos[i]
        for j in range(i + 1, len(photos)):
            b = photos[j]
            if a.get("unit_id") == b.get("unit_id"):
                continue
            distance = hamming(a["dhash"], b["dhash"])
            if distance > max_distance:
                continue

            addr_a = normalize_address(a.get("address"))
            addr_b = normalize_address(b.get("address"))
            same_address = bool(addr_a) and addr_a == addr_b

            price_a, price_b = a.get("price"), b.get("price")
            price_gap = False
            if isinstance(price_a, (int, float)) and isinstance(price_b, (int, float)):
                price_gap = _pct_diff(price_a, price_b) > PRICE_GAP_PCT_THRESHOLD

            zombie_repost = a.get("live") is False or b.get("live") is False

            if same_address and not price_gap and not zombie_repost:
                continue  # expected: shared building/amenity photo, not a scam signal

            flags = []
            if not same_address:
                flags.append("cross_address")
            if a.get("platform") != b.get("platform"):
                flags.append("cross_platform")
            if price_gap:
                flags.append("price_gap")
            if zombie_repost:
                flags.append("zombie_repost")

            matches.append(
                {
                    "a_unit": a.get("unit_id"),
                    "b_unit": b.get("unit_id"),
                    "distance": distance,
                    "flags": flags,
                }
            )

    matches.sort(key=lambda m: (m["distance"], m["a_unit"] or "", m["b_unit"] or ""))
    return matches


def photo_coverage(index: dict) -> dict:
    """Per-unit photo coverage tally: how many photos each unit_id has in
    the index and how many hashed successfully. Used by the run report so
    a unit with zero archived photos is reported as "n/a", never presented
    as an implicit clean photo check."""
    coverage: dict[str, dict] = {}
    for row in index.get("photos", []):
        uid = row.get("unit_id")
        if uid is None:
            continue
        c = coverage.setdefault(uid, {"photos": 0, "hashed": 0})
        c["photos"] += 1
        if row.get("dhash"):
            c["hashed"] += 1
    return coverage


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: photo_hash.py index manifest.json", file=sys.stderr)
        print("       photo_hash.py scan index.json [max_distance]", file=sys.stderr)
        return 2

    command, path = sys.argv[1], sys.argv[2]

    if command == "index":
        manifest = json.loads(pathlib.Path(path).read_text())
        index = build_index(manifest)
        json.dump(index, sys.stdout, indent=1)
        print()
        return 0

    if command == "scan":
        index = json.loads(pathlib.Path(path).read_text())
        max_distance = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_MAX_DISTANCE
        result = {
            "matches": find_matches(index, max_distance=max_distance),
            "coverage": photo_coverage(index),
        }
        json.dump(result, sys.stdout, indent=1)
        print()
        return 0

    print(f"unknown command: {command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
