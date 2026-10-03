"""Tests for walk.py - Walk Score URL, page parsing, and the cache.
Every fetch is an injected fake; no network calls anywhere in this file.
"""

from __future__ import annotations

import json
import pathlib

import walk

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "walk"
PAGE = (FIXTURES / "score_page.html").read_bytes()
NOW = "2026-10-03T12:00:00+00:00"


def _fetch(body, log=None):
    def fetch(url):
        if log is not None:
            log.append(url)
        if isinstance(body, Exception):
            raise body
        return body
    return fetch


def test_build_score_url_slugs_address():
    assert walk.build_score_url("1 Example Ave.", "Chicago", "IL") == \
        "https://www.walkscore.com/score/1-example-ave-chicago-il"
    assert walk.build_score_url("233 S. Wacker Dr", "New  York", "NY") == \
        "https://www.walkscore.com/score/233-s-wacker-dr-new-york-ny"


def test_parse_score_reads_walk_transit_bike():
    assert walk.parse_score(PAGE.decode()) == {"walk": 92, "transit": 88, "bike": 79}
    alt_only = '<img alt="71 Walk Score of 9 Side St">'
    assert walk.parse_score(alt_only) == {"walk": 71, "transit": None, "bike": None}


def test_parse_score_missing_returns_none():
    none = {"walk": None, "transit": None, "bike": None}
    assert walk.parse_score((FIXTURES / "no_score.html").read_text()) == none
    assert walk.parse_score(None) == none
    # a transit badge alone is not a walk score
    assert walk.parse_score('<img src="/badge/transit/score/60.svg">') == none


def test_walk_score_success_writes_cache(tmp_path):
    cache = tmp_path / "cache" / "walk.json"
    r = walk.walk_score("1 Example Ave", "Chicago", "IL", cache_path=cache, now=NOW, fetch=_fetch(PAGE))
    assert r == {"score": 92, "transit": 88, "bike": 79,
                 "source_url": "https://www.walkscore.com/score/1-example-ave-chicago-il",
                 "fetched_at": "2026-10-03T12:00:00+00:00"}
    stored = json.loads(cache.read_text())
    assert list(stored.values()) == [r]


def test_walk_score_cache_hit_inside_window_skips_fetch(tmp_path):
    cache = tmp_path / "walk.json"
    walk.walk_score("1 Example Ave", "Chicago", "IL", cache_path=cache, now=NOW, fetch=_fetch(PAGE))
    log = []
    r = walk.walk_score("1 Example Ave", "Chicago", "IL", cache_path=cache,
                        now="2026-12-01T12:00:00+00:00", fetch=_fetch(PAGE, log))
    assert log == [] and r["cached"] is True and r["score"] == 92
    assert r["fetched_at"] == "2026-10-03T12:00:00+00:00"


def test_walk_score_cache_expired_refetches(tmp_path):
    cache = tmp_path / "walk.json"
    walk.walk_score("1 Example Ave", "Chicago", "IL", cache_path=cache, now=NOW, fetch=_fetch(PAGE))
    log = []
    later = "2027-01-05T12:00:00+00:00"  # 94 days on, past the 90-day window
    r = walk.walk_score("1 Example Ave", "Chicago", "IL", cache_path=cache, now=later, fetch=_fetch(PAGE, log))
    assert len(log) == 1 and "cached" not in r and r["fetched_at"] == later


def test_walk_score_failure_not_cached(tmp_path):
    cache = tmp_path / "walk.json"
    r = walk.walk_score("1 Example Ave", "Chicago", "IL", cache_path=cache, now=NOW,
                        fetch=_fetch(OSError("connection reset")))
    assert r["score"] is None and r["error"].startswith("fetch failed")
    assert r["source_url"].endswith("1-example-ave-chicago-il") and r["fetched_at"] == NOW
    blocked = walk.walk_score("1 Example Ave", "Chicago", "IL", cache_path=cache, now=NOW,
                              fetch=_fetch((FIXTURES / "no_score.html").read_bytes()))
    assert blocked["score"] is None and "no walk score" in blocked["error"]
    assert not cache.exists()
