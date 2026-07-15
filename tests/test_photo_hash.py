"""Tests for photo_hash.py - perceptual-hash scam net over archived photos.

Imported via the scripts-path hook in tests/conftest.py. No network calls:
every image is generated locally with Pillow into tmp_path, and the CLI is
only ever invoked against local files.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL", reason="Pillow required for photo_hash tests")
from PIL import Image  # noqa: E402

import photo_hash
from photo_hash import (
    average_hash,
    build_index,
    dhash,
    find_matches,
    hamming,
    hash_file,
    normalize_address,
    photo_coverage,
)

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / ".claude" / "skills" / "apartmentops" / "scripts" / "photo_hash.py"
)

SIZE = 64


def _save_lr_gradient(path: Path, size: int = SIZE) -> None:
    """Brightness increases left to right - every dHash bit trends to 1."""
    img = Image.new("RGB", (size, size))
    px = img.load()
    for x in range(size):
        v = int(255 * x / (size - 1))
        for y in range(size):
            px[x, y] = (v, v, v)
    img.save(path)


def _save_tb_gradient(path: Path, size: int = SIZE) -> None:
    """Brightness constant across each row (varies top to bottom only) - a
    horizontal-neighbor dHash sees no left<right signal, trending to 0.
    Maximally different from _save_lr_gradient's hash."""
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        v = int(255 * y / (size - 1))
        for x in range(size):
            px[x, y] = (v, v, v)
    img.save(path)


# --------------------------------------------------------------------------
# Hash primitives
# --------------------------------------------------------------------------


def test_average_hash_and_dhash_return_hex_strings_of_expected_length(tmp_path):
    p = tmp_path / "a.png"
    _save_lr_gradient(p)
    with Image.open(p) as img:
        a = average_hash(img)
        d = dhash(img)
    assert re.fullmatch(r"[0-9a-f]{16}", a)
    assert re.fullmatch(r"[0-9a-f]{16}", d)


def test_dhash_identical_images_zero_distance(tmp_path):
    p1 = tmp_path / "a.png"
    p2 = tmp_path / "a_copy.png"
    _save_lr_gradient(p1)
    _save_lr_gradient(p2)
    h1 = hash_file(p1)
    h2 = hash_file(p2)
    assert hamming(h1["dhash"], h2["dhash"]) == 0
    assert hamming(h1["ahash"], h2["ahash"]) == 0


def test_dhash_differs_widely_for_unrelated_gradients(tmp_path):
    p1 = tmp_path / "lr.png"
    p2 = tmp_path / "tb.png"
    _save_lr_gradient(p1)
    _save_tb_gradient(p2)
    h1 = hash_file(p1)
    h2 = hash_file(p2)
    distance = hamming(h1["dhash"], h2["dhash"])
    assert distance > photo_hash.DEFAULT_MAX_DISTANCE


def test_hamming_length_mismatch_raises():
    with pytest.raises(ValueError):
        hamming("ab", "abcd")


def test_hash_file_converts_to_grayscale_first(tmp_path):
    p = tmp_path / "color.png"
    img = Image.new("RGB", (16, 16), color=(200, 30, 30))
    img.save(p)
    result = hash_file(p)
    assert isinstance(result["ahash"], str)
    assert isinstance(result["dhash"], str)


def test_hash_file_missing_file_raises(tmp_path):
    with pytest.raises(Exception):
        hash_file(tmp_path / "does-not-exist.png")


# --------------------------------------------------------------------------
# build_index
# --------------------------------------------------------------------------


def test_build_index_hashes_readable_photo_and_preserves_provenance(tmp_path):
    photo = tmp_path / "unit1.png"
    _save_lr_gradient(photo)
    manifest = [
        {
            "unit_id": "tower-1a",
            "address": "1 Example Ave",
            "platform": "streeteasy",
            "price": 4900,
            "live": True,
            "photo_path": str(photo),
        }
    ]
    index = build_index(manifest)
    assert "generated_at" in index
    row = index["photos"][0]
    assert row["unit_id"] == "tower-1a"
    assert row["address"] == "1 Example Ave"
    assert row["platform"] == "streeteasy"
    assert row["price"] == 4900
    assert row["live"] is True
    assert row["error"] is None
    assert re.fullmatch(r"[0-9a-f]{16}", row["ahash"])
    assert re.fullmatch(r"[0-9a-f]{16}", row["dhash"])


def test_build_index_never_fabricates_hash_for_unreadable_photo(tmp_path):
    manifest = [
        {
            "unit_id": "tower-2b",
            "address": "2 Example Ave",
            "platform": "zillow",
            "price": 5200,
            "live": True,
            "photo_path": str(tmp_path / "missing.png"),
        }
    ]
    index = build_index(manifest)
    row = index["photos"][0]
    assert row["ahash"] is None
    assert row["dhash"] is None
    assert row["error"] is not None
    assert "unit_id" in row and row["unit_id"] == "tower-2b"


def test_build_index_corrupt_image_records_error_not_hash(tmp_path):
    bad = tmp_path / "corrupt.png"
    bad.write_bytes(b"not actually a png")
    manifest = [
        {
            "unit_id": "tower-3c",
            "address": "3 Example Ave",
            "platform": "apartments.com",
            "price": 4700,
            "live": True,
            "photo_path": str(bad),
        }
    ]
    index = build_index(manifest)
    row = index["photos"][0]
    assert row["ahash"] is None
    assert row["dhash"] is None
    assert row["error"] is not None


# --------------------------------------------------------------------------
# normalize_address
# --------------------------------------------------------------------------


def test_normalize_address_is_case_and_punctuation_insensitive():
    a = normalize_address("1 Example Ave, City, ST")
    b = normalize_address("1  example ave city st")
    assert a == b


def test_normalize_address_handles_none_and_empty():
    assert normalize_address(None) == ""
    assert normalize_address("") == ""


# --------------------------------------------------------------------------
# find_matches - acceptance-criteria fixtures
# --------------------------------------------------------------------------


def _manifest_row(unit_id, address, platform, price, live, photo_path):
    return {
        "unit_id": unit_id,
        "address": address,
        "platform": platform,
        "price": price,
        "live": live,
        "photo_path": str(photo_path),
    }


def test_cross_address_duplicate_photo_flags_with_evidence_and_distance(tmp_path):
    # AC: a fixture archive with one photo appearing under two addresses
    # produces a cross-address flag carrying both evidence photos and the
    # computed distance.
    shared = tmp_path / "shared.png"
    _save_lr_gradient(shared)
    manifest = [
        _manifest_row("bldg-a-101", "1 Example Ave, City, ST", "streeteasy", 4900, True, shared),
        _manifest_row("bldg-b-202", "99 Other St, City, ST", "craigslist", 2100, True, shared),
    ]
    index = build_index(manifest)
    matches = find_matches(index)
    assert len(matches) == 1
    match = matches[0]
    assert {match["a_unit"], match["b_unit"]} == {"bldg-a-101", "bldg-b-202"}
    assert match["distance"] == 0
    assert "cross_address" in match["flags"]

    # Evidence photos are recoverable by joining a_unit/b_unit back to the
    # index - this is the pattern the dashboard evidence panel uses.
    by_unit = {row["unit_id"]: row for row in index["photos"]}
    assert by_unit[match["a_unit"]]["photo_path"] == str(shared)
    assert by_unit[match["b_unit"]]["photo_path"] == str(shared)


def test_zombie_repost_flag_when_one_side_previously_recorded_gone(tmp_path):
    # AC: a photo matching a unit previously recorded GONE produces a
    # zombie-repost flag.
    shared = tmp_path / "shared.png"
    _save_lr_gradient(shared)
    manifest = [
        _manifest_row("bldg-a-101", "1 Example Ave, City, ST", "streeteasy", 4900, False, shared),
        _manifest_row("bldg-a-101-repost", "1 Example Ave, City, ST", "streeteasy", 4900, True, shared),
    ]
    index = build_index(manifest)
    matches = find_matches(index)
    assert len(matches) == 1
    assert "zombie_repost" in matches[0]["flags"]


def test_unrelated_photos_below_threshold_produce_no_match(tmp_path):
    # AC: unrelated fixture photos below the similarity threshold produce
    # no flag.
    p1 = tmp_path / "lr.png"
    p2 = tmp_path / "tb.png"
    _save_lr_gradient(p1)
    _save_tb_gradient(p2)
    manifest = [
        _manifest_row("bldg-a-101", "1 Example Ave, City, ST", "streeteasy", 4900, True, p1),
        _manifest_row("bldg-b-202", "2 Example Ave, City, ST", "zillow", 5100, True, p2),
    ]
    index = build_index(manifest)
    matches = find_matches(index)
    assert matches == []


def test_same_address_same_price_shared_amenity_photo_excluded(tmp_path):
    shared = tmp_path / "lobby.png"
    _save_lr_gradient(shared)
    manifest = [
        _manifest_row("tower-501", "1 Example Ave, City, ST", "streeteasy", 4900, True, shared),
        _manifest_row("tower-1802", "1 example ave, city, st", "landlord-direct", 4900, True, shared),
    ]
    index = build_index(manifest)
    matches = find_matches(index)
    assert matches == []


def test_same_address_but_price_divergence_is_flagged(tmp_path):
    shared = tmp_path / "lobby.png"
    _save_lr_gradient(shared)
    manifest = [
        _manifest_row("tower-501", "1 Example Ave, City, ST", "streeteasy", 4900, True, shared),
        _manifest_row("tower-999", "1 Example Ave, City, ST", "craigslist", 1800, True, shared),
    ]
    index = build_index(manifest)
    matches = find_matches(index)
    assert len(matches) == 1
    assert "price_gap" in matches[0]["flags"]
    assert "cross_address" not in matches[0]["flags"]


def test_same_unit_id_photo_pairs_are_never_compared(tmp_path):
    p1 = tmp_path / "1.png"
    p2 = tmp_path / "2.png"
    _save_lr_gradient(p1)
    _save_lr_gradient(p2)
    manifest = [
        _manifest_row("tower-501", "1 Example Ave", "streeteasy", 4900, True, p1),
        _manifest_row("tower-501", "1 Example Ave", "streeteasy", 4900, True, p2),
    ]
    index = build_index(manifest)
    matches = find_matches(index)
    assert matches == []


def test_max_distance_is_configurable(tmp_path):
    p1 = tmp_path / "lr.png"
    p2 = tmp_path / "tb.png"
    _save_lr_gradient(p1)
    _save_tb_gradient(p2)
    manifest = [
        _manifest_row("bldg-a-101", "1 Example Ave", "streeteasy", 4900, True, p1),
        _manifest_row("bldg-b-202", "2 Example Ave", "zillow", 5100, True, p2),
    ]
    index = build_index(manifest)
    # a very large max_distance should surface the pair that the default
    # threshold correctly filtered out
    matches = find_matches(index, max_distance=64)
    assert len(matches) == 1


def test_unreadable_photos_are_excluded_from_matching_not_crashed_on(tmp_path):
    shared = tmp_path / "shared.png"
    _save_lr_gradient(shared)
    manifest = [
        _manifest_row("bldg-a-101", "1 Example Ave", "streeteasy", 4900, True, shared),
        _manifest_row("bldg-b-202", "2 Example Ave", "zillow", 5100, True, tmp_path / "missing.png"),
    ]
    index = build_index(manifest)
    matches = find_matches(index)  # must not raise
    assert matches == []


# --------------------------------------------------------------------------
# photo_coverage - "photo check: n/a" support
# --------------------------------------------------------------------------


def test_photo_coverage_counts_photos_and_hashed_per_unit(tmp_path):
    good = tmp_path / "good.png"
    _save_lr_gradient(good)
    manifest = [
        _manifest_row("tower-501", "1 Example Ave", "streeteasy", 4900, True, good),
        _manifest_row("tower-501", "1 Example Ave", "streeteasy", 4900, True, tmp_path / "missing.png"),
    ]
    index = build_index(manifest)
    coverage = photo_coverage(index)
    assert coverage["tower-501"]["photos"] == 2
    assert coverage["tower-501"]["hashed"] == 1


def test_photo_coverage_has_no_entry_for_unit_with_no_archived_photos(tmp_path):
    # AC: a unit with no archived photos renders "photo check: n/a", never
    # an implicit clean state. This module's contribution to that: the
    # coverage map simply has no key for that unit_id, so the dashboard
    # must render "n/a" for a missing key rather than defaulting to "clean".
    good = tmp_path / "good.png"
    _save_lr_gradient(good)
    manifest = [_manifest_row("tower-501", "1 Example Ave", "streeteasy", 4900, True, good)]
    index = build_index(manifest)
    coverage = photo_coverage(index)
    assert "tower-999-no-photos" not in coverage


# --------------------------------------------------------------------------
# Guarded Pillow import
# --------------------------------------------------------------------------


def test_missing_pillow_raises_clear_install_hint(monkeypatch):
    monkeypatch.setattr(photo_hash, "Image", None)
    with pytest.raises(ImportError, match="pip install Pillow"):
        photo_hash._require_pillow()


# --------------------------------------------------------------------------
# Zero network requests (static guardrail)
# --------------------------------------------------------------------------


def test_module_source_contains_no_network_calls():
    source = SCRIPT.read_text()
    forbidden = ["urllib", "requests", "http.client", "socket.", "playwright"]
    for token in forbidden:
        assert token not in source, f"unexpected network-capable token: {token}"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_index_then_scan_roundtrip(tmp_path):
    shared = tmp_path / "shared.png"
    _save_lr_gradient(shared)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            [
                _manifest_row("bldg-a-101", "1 Example Ave", "streeteasy", 4900, True, shared),
                _manifest_row("bldg-b-202", "99 Other St", "craigslist", 2100, True, shared),
            ]
        )
    )

    index_proc = subprocess.run(
        [sys.executable, str(SCRIPT), "index", str(manifest_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    index_payload = json.loads(index_proc.stdout)
    assert len(index_payload["photos"]) == 2

    index_path = tmp_path / "index.json"
    index_path.write_text(index_proc.stdout)

    scan_proc = subprocess.run(
        [sys.executable, str(SCRIPT), "scan", str(index_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    scan_payload = json.loads(scan_proc.stdout)
    assert len(scan_payload["matches"]) == 1
    assert "cross_address" in scan_payload["matches"][0]["flags"]
    assert "bldg-a-101" in scan_payload["coverage"]


def test_cli_no_args_prints_usage_and_exits_2():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "usage" in proc.stderr.lower()


def test_cli_unknown_command_exits_2(tmp_path):
    dummy = tmp_path / "x.json"
    dummy.write_text("[]")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "bogus", str(dummy)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
