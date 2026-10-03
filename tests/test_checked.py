"""Tests for checked.py - rejection memory, scan coverage, drain file."""

from __future__ import annotations

import json

import pytest

import checked

NOW = "2026-10-03T12:00:00+00:00"


def test_fingerprint_ignores_non_criteria_keys():
    a = {"budget": {"gross_max": 3000}, "anchor": {"label": "Office"}}
    b = {"budget": {"gross_max": 3000}, "anchor": {"label": "HQ"}}
    c = {"budget": {"gross_max": 3200}}
    assert checked.criteria_fingerprint(a) == checked.criteria_fingerprint(b)
    assert checked.criteria_fingerprint(a) != checked.criteria_fingerprint(c)


def test_rejection_expires_on_criteria_change_and_age():
    s = checked.empty_state()
    checked.record_rejection(s, "t1", "over budget", "2026-10-01T00:00:00+00:00", "fp1")
    assert checked.rejection_active(s, "t1", "fp1", NOW)
    assert not checked.rejection_active(s, "t1", "fp2", NOW)
    assert not checked.rejection_active(s, "t1", "fp1", "2026-12-01T00:00:00+00:00")


def test_criteria_independent_rejection_survives_criteria_change():
    s = checked.empty_state()
    checked.record_rejection(s, "t1", "sublet", "2026-10-01T00:00:00+00:00", "fp1",
                             criteria_dependent=False)
    assert checked.rejection_active(s, "t1", "fp2", NOW)


def test_naive_timestamp_refused():
    s = checked.empty_state()
    with pytest.raises(ValueError):
        checked.record_rejection(s, "t1", "x", "2026-10-01T00:00:00", "fp")


def test_filter_new_splits_and_dedupes():
    s = checked.empty_state()
    checked.record_rejection(s, "r", "shared room", "2026-10-02T00:00:00+00:00", "fp")
    checked.record_out_of_window(s, "old", "2025-01-01T00:00:00+00:00", NOW)
    out = checked.filter_new(["a", "r", "a", "old", "k", "b"], ["k"], s, "fp", NOW)
    assert out["open"] == ["a", "b"]
    assert out["skipped"]["k"] == "already tracked"
    assert out["skipped"]["r"].startswith("rejected")
    assert out["skipped"]["old"] == "out of publication window"


def test_coverage_complete_only_when_every_page_read_and_no_blocker():
    s = checked.empty_state()
    checked.start_scan(s, "run1", NOW)
    checked.set_pages_expected(s, "run1", "area-a", 3)
    for p in (1, 2, 3):
        checked.mark_page(s, "run1", "area-a", p)
    assert not checked.coverage(s, "run1")["complete"]  # not finished yet
    checked.finish_scan(s, "run1", NOW)
    assert checked.coverage(s, "run1")["complete"]

    checked.start_scan(s, "run2", NOW)
    checked.set_pages_expected(s, "run2", "area-a", 3)
    checked.mark_page(s, "run2", "area-a", 1)
    checked.add_blocker(s, "run2", "captcha", "bot manager page", NOW)
    checked.finish_scan(s, "run2", NOW)
    cov = checked.coverage(s, "run2")
    assert not cov["complete"]
    assert cov["queries"][0]["missing_pages"] == [2, 3]


def test_coverage_without_page_count_is_incomplete():
    s = checked.empty_state()
    checked.start_scan(s, "r", NOW)
    checked.mark_page(s, "r", "q", 1)
    checked.finish_scan(s, "r", NOW)
    assert not checked.coverage(s, "r")["complete"]


def test_save_load_roundtrip(tmp_path):
    s = checked.empty_state()
    checked.record_deferred(s, "out_of_area", "t9", "outside geography", NOW)
    path = tmp_path / "data" / "checked.json"
    checked.save(path, s)
    assert checked.load(path)["deferred"]["out_of_area"]["t9"]["reason"] == "outside geography"
    assert checked.load(tmp_path / "nope.json") == checked.empty_state()


def test_drain_append_and_merge(tmp_path):
    p = tmp_path / "drain.jsonl"
    checked.drain_append(p, [{"token": "a", "price": 100}, {"token": "b", "price": 200}])
    checked.drain_append(p, [{"token": "a", "published_at": "2026-10-01T09:00:00+03:00"}])
    with p.open("a") as fh:
        fh.write('{"token": "c", "pri')  # crash mid-write
    got = checked.load_drained(p)
    assert got["a"] == {"token": "a", "price": 100, "published_at": "2026-10-01T09:00:00+03:00"}
    assert set(got) == {"a", "b"}


def test_drain_rejects_tokenless_batch_atomically(tmp_path):
    p = tmp_path / "drain.jsonl"
    with pytest.raises(ValueError):
        checked.drain_append(p, [{"token": "a"}, {"price": 1}])
    assert not p.exists()
