"""Tests for flood.py - keyless FEMA NFHL point lookups.

No network calls: every test supplies a fake `fetch` callable so the
module's HTTP layer is never exercised.
"""

from __future__ import annotations

import json

import flood


def _payload(zone="AE", subtype="", sfha="T"):
    return json.dumps(
        {
            "features": [
                {"attributes": {"FLD_ZONE": zone, "ZONE_SUBTY": subtype, "SFHA_TF": sfha}}
            ]
        }
    ).encode()


def test_success_returns_zone_subtype_sfha_and_urls():
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return _payload(zone="AE", subtype="FLOODWAY", sfha="T")

    result = flood.flood_zone(40.7357, -74.0270, fetch=fake_fetch)

    assert result["zone"] == "AE"
    assert result["subtype"] == "FLOODWAY"
    assert result["sfha"] is True
    assert result["source_url"] == calls[0]
    assert "hazards.fema.gov" in result["source_url"]
    assert "geometry=-74.027" in result["source_url"] or "-74.027" in result["source_url"]
    assert result["viewer_url"].startswith("https://msc.fema.gov/portal/search")
    assert "fetched_at" in result and result["fetched_at"]
    assert "error" not in result


def test_zone_x_with_false_sfha():
    def fake_fetch(url):
        return _payload(zone="X", subtype="0.2 PCT ANNUAL CHANCE FLOOD HAZARD", sfha="F")

    result = flood.flood_zone(41.0, -73.0, fetch=fake_fetch)

    assert result["zone"] == "X"
    assert result["sfha"] is False


def test_unparseable_sfha_value_yields_null_but_zone_still_resolves():
    def fake_fetch(url):
        return _payload(zone="AE", subtype="", sfha="MAYBE")

    result = flood.flood_zone(40.0, -74.0, fetch=fake_fetch)

    assert result["zone"] == "AE"
    assert result["sfha"] is None
    assert "error" not in result


def test_empty_features_is_a_failure_not_no_risk():
    def fake_fetch(url):
        return json.dumps({"features": []}).encode()

    result = flood.flood_zone(40.0, -74.0, fetch=fake_fetch)

    assert result["zone"] is None
    assert "error" in result
    assert "empty result" in result["error"]
    # Never silently defaulted to a "no risk" zone like "X".
    assert result["zone"] != "X"


def test_missing_flood_zone_field_is_a_failure():
    def fake_fetch(url):
        return json.dumps(
            {"features": [{"attributes": {"FLD_ZONE": "", "ZONE_SUBTY": "", "SFHA_TF": "F"}}]}
        ).encode()

    result = flood.flood_zone(40.0, -74.0, fetch=fake_fetch)

    assert result["zone"] is None
    assert "error" in result
    assert "FLD_ZONE" in result["error"]


def test_missing_coordinates_never_calls_fetch():
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return _payload()

    result = flood.flood_zone(None, -74.0, fetch=fake_fetch)

    assert result["zone"] is None
    assert result["error"] == "missing coordinates"
    assert result["source_url"] is None
    assert result["viewer_url"] is None
    assert calls == []


def test_fetch_exception_becomes_captured_failure():
    def raising_fetch(url):
        raise TimeoutError("simulated timeout")

    result = flood.flood_zone(40.0, -74.0, fetch=raising_fetch)

    assert result["zone"] is None
    assert "TimeoutError" in result["error"]
    assert "simulated timeout" in result["error"]
    # source_url and viewer_url are still built even though the fetch failed.
    assert result["source_url"] is not None
    assert result["viewer_url"] is not None


def test_malformed_json_response_is_a_failure():
    def fake_fetch(url):
        return b"not json at all {{{"

    result = flood.flood_zone(40.0, -74.0, fetch=fake_fetch)

    assert result["zone"] is None
    assert "error" in result


def test_service_error_response_is_a_failure():
    def fake_fetch(url):
        return json.dumps({"error": {"code": 500, "message": "boom"}}).encode()

    result = flood.flood_zone(40.0, -74.0, fetch=fake_fetch)

    assert result["zone"] is None
    assert "service error" in result["error"]


def test_cache_hit_skips_network(tmp_path):
    cache_path = str(tmp_path / "flood-cache.json")
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return _payload(zone="AE", sfha="T")

    first = flood.flood_zone(40.1234, -74.5678, fetch=fake_fetch, cache_path=cache_path)
    assert len(calls) == 1
    assert first["zone"] == "AE"

    second = flood.flood_zone(40.1234, -74.5678, fetch=fake_fetch, cache_path=cache_path)
    assert len(calls) == 1, "cache hit must not call fetch again"
    assert second == first


def test_cache_key_rounds_to_four_decimals(tmp_path):
    cache_path = str(tmp_path / "flood-cache.json")
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return _payload(zone="X")

    flood.flood_zone(40.123456, -74.567891, fetch=fake_fetch, cache_path=cache_path)
    # A point rounding to the same 4-decimal key should hit the cache.
    flood.flood_zone(40.1234561, -74.5678911, fetch=fake_fetch, cache_path=cache_path)

    assert len(calls) == 1


def test_failures_are_never_cached(tmp_path):
    cache_path = str(tmp_path / "flood-cache.json")
    calls = []

    def failing_then_succeeding_fetch(url):
        calls.append(url)
        if len(calls) == 1:
            raise ConnectionError("simulated outage")
        return _payload(zone="AE")

    first = flood.flood_zone(40.0, -74.0, fetch=failing_then_succeeding_fetch, cache_path=cache_path)
    assert first["zone"] is None

    second = flood.flood_zone(40.0, -74.0, fetch=failing_then_succeeding_fetch, cache_path=cache_path)
    assert second["zone"] == "AE"
    assert len(calls) == 2, "a failed lookup must retry on the next call, not be cached"


def test_query_url_uses_lon_lat_order_and_required_fields():
    def fake_fetch(url):
        return _payload()

    result = flood.flood_zone(40.7357, -74.0270, fetch=fake_fetch)
    url = result["source_url"]

    assert "geometryType=esriGeometryPoint" in url
    assert "inSR=4326" in url
    assert "outFields=FLD_ZONE%2CZONE_SUBTY%2CSFHA_TF" in url or "FLD_ZONE" in url
    assert "returnGeometry=false" in url
    assert "f=json" in url
    # geometry is "lon,lat" order per the ticket's binding design.
    assert "geometry=-74.027,40.7357" in url


def test_default_fetch_is_used_when_none_supplied(monkeypatch):
    called = {}

    def fake_default_fetch(url):
        called["url"] = url
        return _payload(zone="AE")

    monkeypatch.setattr(flood, "default_fetch", fake_default_fetch)
    result = flood.flood_zone(40.0, -74.0)

    assert called["url"]
    assert result["zone"] == "AE"


def test_cli_usage_error_without_network(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["flood.py", "onlyonearg"])
    rc = flood.main()
    captured = capsys.readouterr()

    assert rc == 2
    assert "usage" in captured.err
