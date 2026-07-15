"""Tests for doctor_searches.py: saved-search preflight. All fetches are
injected fakes - no real network calls anywhere in this file.
"""

from __future__ import annotations

import json

import pytest

import doctor_searches as ds


def _fetch_map(responses: dict[str, tuple[int, str]], call_log: list[str] | None = None):
    """Build a fake fetch(url) -> (status, body) from a url -> response map.
    Records every call in call_log (if given) so tests can assert a bot
    wall was never retried."""
    def fetch(url: str) -> tuple[int, str]:
        if call_log is not None:
            call_log.append(url)
        if url not in responses:
            raise AssertionError(f"unexpected fetch of {url!r}")
        return responses[url]
    return fetch


# ---------------------------------------------------------------------------
# check_saved_searches
# ---------------------------------------------------------------------------

def test_pass_with_marker_match():
    saved = {"primary": {"platform_a": {"area_x": {
        "url": "https://example.test/search?area=x",
        "marker": r'"result_count":\s*\d+',
    }}}}
    fetch = _fetch_map({"https://example.test/search?area=x": (200, '{"result_count": 14}')})
    rows = ds.check_saved_searches(saved, fetch=fetch)
    assert len(rows) == 1
    row = rows[0]
    assert row["ok"] is True
    assert row["status"] == 200
    assert row["profile"] == "primary"
    assert row["platform"] == "platform_a"
    assert row["area"] == "area_x"


def test_fail_with_marker_no_match():
    saved = {"primary": {"platform_a": {"area_x": {
        "url": "https://example.test/search?area=x",
        "marker": r'"result_count":\s*[1-9]',
    }}}}
    fetch = _fetch_map({"https://example.test/search?area=x": (200, '{"result_count": 0}')})
    rows = ds.check_saved_searches(saved, fetch=fetch)
    assert rows[0]["ok"] is False
    assert "did not match" in rows[0]["detail"]


def test_pass_without_marker_long_body():
    saved = {"primary": {"platform_a": {"area_x": "https://example.test/search"}}}
    fetch = _fetch_map({"https://example.test/search": (200, "x" * 5000)})
    rows = ds.check_saved_searches(saved, fetch=fetch, body_length_threshold=2000)
    assert rows[0]["ok"] is True
    assert "marker form is strongly preferred" in rows[0]["detail"]


def test_fail_without_marker_short_body():
    saved = {"primary": {"platform_a": {"area_x": "https://example.test/search"}}}
    fetch = _fetch_map({"https://example.test/search": (200, "short")})
    rows = ds.check_saved_searches(saved, fetch=fetch, body_length_threshold=2000)
    assert rows[0]["ok"] is False


def test_non_200_status_fails():
    saved = {"primary": {"platform_a": {"area_x": "https://example.test/search"}}}
    fetch = _fetch_map({"https://example.test/search": (500, "server error")})
    rows = ds.check_saved_searches(saved, fetch=fetch)
    assert rows[0]["ok"] is False
    assert "500" in rows[0]["detail"]


def test_403_is_never_retried_and_reports_bot_wall():
    saved = {"primary": {"platform_a": {"area_x": "https://example.test/search"}}}
    call_log: list[str] = []
    fetch = _fetch_map({"https://example.test/search": (403, "blocked")}, call_log)
    rows = ds.check_saved_searches(saved, fetch=fetch)
    assert rows[0]["ok"] is False
    assert rows[0]["status"] == 403
    assert "bot wall" in rows[0]["detail"]
    assert call_log == ["https://example.test/search"]  # exactly one call, no retry


def test_missing_url_reports_no_url_configured():
    saved = {"primary": {"platform_a": {"area_x": None}}}
    rows = ds.check_saved_searches(saved, fetch=_fetch_map({}))
    assert rows[0]["ok"] is False
    assert "no url configured" in rows[0]["detail"]


def test_invalid_marker_regex_reports_detail_not_crash():
    saved = {"primary": {"platform_a": {"area_x": {
        "url": "https://example.test/search",
        "marker": "[unclosed",
    }}}}
    fetch = _fetch_map({"https://example.test/search": (200, "body")})
    rows = ds.check_saved_searches(saved, fetch=fetch)
    assert rows[0]["ok"] is False
    assert "invalid marker regex" in rows[0]["detail"]


def test_multiple_profiles_platforms_areas_all_reported():
    saved = {
        "primary": {
            "platform_a": {
                "area_x": {"url": "https://example.test/a-x", "marker": "OK"},
                "area_y": {"url": "https://example.test/a-y", "marker": "OK"},
            },
        },
        "fallback_1br": {
            "platform_a": {
                "area_x": {"url": "https://example.test/fb-a-x", "marker": "OK"},
            },
        },
    }
    fetch = _fetch_map({
        "https://example.test/a-x": (200, "OK here"),
        "https://example.test/a-y": (200, "nope"),
        "https://example.test/fb-a-x": (200, "OK here"),
    })
    rows = ds.check_saved_searches(saved, fetch=fetch)
    assert len(rows) == 3
    by_key = {(r["profile"], r["platform"], r["area"]): r for r in rows}
    assert by_key[("primary", "platform_a", "area_x")]["ok"] is True
    assert by_key[("primary", "platform_a", "area_y")]["ok"] is False
    assert by_key[("fallback_1br", "platform_a", "area_x")]["ok"] is True


def test_empty_saved_searches_yields_empty_rows():
    assert ds.check_saved_searches({}, fetch=_fetch_map({})) == []


def test_default_fetch_is_used_when_none_given(monkeypatch):
    calls = []

    def fake_default_fetch(url):
        calls.append(url)
        return 200, "OK"

    monkeypatch.setattr(ds, "default_fetch", fake_default_fetch)
    saved = {"primary": {"platform_a": {"area_x": {"url": "https://example.test/x", "marker": "OK"}}}}
    rows = ds.check_saved_searches(saved)  # fetch=None -> should use ds.default_fetch
    assert calls == ["https://example.test/x"]
    assert rows[0]["ok"] is True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_end_to_end_json_saved_searches_block(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "saved_searches.json"
    config_path.write_text(json.dumps({
        "primary": {"platform_a": {"area_x": {"url": "https://example.test/x", "marker": "OK"}}},
    }))

    def fake_fetch(url):
        return 200, "OK here"

    monkeypatch.setattr(ds, "default_fetch", fake_fetch)
    rc = ds.main([str(config_path)])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["ok"] is True


def test_cli_end_to_end_full_config_with_saved_searches_key(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "version": 1,
        "saved_searches": {
            "primary": {"platform_a": {"area_x": {"url": "https://example.test/x", "marker": "OK"}}},
        },
    }))

    def fake_fetch(url):
        return 500, "nope"

    monkeypatch.setattr(ds, "default_fetch", fake_fetch)
    rc = ds.main([str(config_path)])
    assert rc == 1  # a failed row -> nonzero exit
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["ok"] is False


def test_cli_yaml_config(tmp_path, capsys, monkeypatch):
    pytest.importorskip("yaml")
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "saved_searches:\n"
        "  primary:\n"
        "    platform_a:\n"
        "      area_x:\n"
        "        url: https://example.test/x\n"
        "        marker: 'OK'\n"
    )

    def fake_fetch(url):
        return 200, "OK here"

    monkeypatch.setattr(ds, "default_fetch", fake_fetch)
    rc = ds.main([str(config_path)])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["ok"] is True


def test_cli_wrong_arg_count(capsys):
    rc = ds.main([])
    assert rc == 2
    assert "usage" in capsys.readouterr().err.lower()
