"""Tests for the three-state liveness verdict in verify_units.py.

Only the pure functions are exercised here (classify, the shared-shell
rule, policy matching, price-layer detection); nothing launches a browser.

Run with: python -m pytest tests/test_verify_units.py -q (from the repo root)
"""

from __future__ import annotations

import json

import pytest

from verify_units import (
    SHELL_MIN_CHARS,
    SHELL_SHARED_MIN,
    apply_shell_rule,
    classify,
    detect_wall,
    infer_kind,
    is_label_token,
    live_from_verdict,
    load_sources,
    policy_for,
    price_layer,
    token_pattern,
)

FULL = "Building amenities and leasing office hours. " * 20  # ~900 chars of filler
OWN_URL = "https://operator.test/floorplans/unit-2207"
INDEX_URL = "https://operator.test/availability"

TABLE_POLICY = {"match": "/availability", "kind": "table", "complete": True,
                "live_evidence": True, "gone_evidence": True, "price_evidence": True,
                "price_layer": "base"}
OWN_POLICY = {"match": "/floorplans/unit-", "kind": "own_page",
              "live_evidence": True, "gone_evidence": True, "price_evidence": True}
UNTRUSTED_POLICY = {"match": "/inventory/unit/", "kind": "untrusted",
                    "live_evidence": False, "gone_evidence": False, "price_evidence": False,
                    "note": "renders any invented unit id with a price"}


def _table_body(*rows: str) -> str:
    return "Available residences\n" + FULL + "\n" + "\n".join(rows) + "\n" + FULL


# --------------------------------------------------------------------------
# token and label helpers
# --------------------------------------------------------------------------


def test_token_pattern_tolerates_zero_padding():
    pat = token_pattern("407")
    assert pat.search("Unit 0407 available")
    assert pat.search("Residence 407")
    assert not pat.search("Unit 15030")


def test_label_tokens_are_never_unit_numbers():
    assert is_label_token("2BR-3")
    assert is_label_token("Studio")
    assert is_label_token("2 BR")
    assert is_label_token("")
    assert not is_label_token("2207")
    assert not is_label_token("PH1E")
    assert not is_label_token("S-1902")


def test_live_from_verdict_mapping():
    assert live_from_verdict("live") is True
    assert live_from_verdict("gone") is False
    assert live_from_verdict("check") is None


# --------------------------------------------------------------------------
# index and root pages: live only, never gone
# --------------------------------------------------------------------------


def test_index_with_token_is_live_but_price_not_admissible():
    body = _table_body("2207  2 Bed / 2 Bath  $4,380  Available Sep 2")
    r = classify(INDEX_URL, INDEX_URL, body, "2207", {"kind": "index"})
    assert r["verdict"] == "live"
    assert r["token_found"] is True
    assert r["price"] is None
    assert r["price_trust"] == "none"


def test_index_without_token_is_check_not_gone():
    r = classify(INDEX_URL, INDEX_URL, _table_body("1704  $5,100"), "2207", {"kind": "index"})
    assert r["verdict"] == "check"
    assert "cannot prove gone" in r["why"]


def test_root_url_without_policy_is_an_index():
    assert infer_kind("https://operator.test/", "https://operator.test/", "2207") == "index"
    r = classify("https://operator.test/", "https://operator.test/", FULL, "2207")
    assert r["kind"] == "index"
    assert r["verdict"] == "check"


def test_policy_less_unit_url_is_own_page_and_can_go_gone():
    assert infer_kind(OWN_URL, OWN_URL, "2207") == "own_page"
    r = classify(OWN_URL, OWN_URL, FULL, "2207")
    assert r["kind"] == "own_page"
    assert r["verdict"] == "gone"


def test_policy_less_unknown_path_cannot_prove_gone():
    url = "https://operator.test/some/page"
    r = classify(url, url, FULL, "2207")
    assert r["kind"] == "unknown"
    assert r["verdict"] == "check"


# --------------------------------------------------------------------------
# walls, shells, fetch failures: prior verdict kept
# --------------------------------------------------------------------------


def test_login_redirect_is_a_wall_even_on_an_own_page():
    final = "https://operator.test/account/login?next=/floorplans/unit-2207"
    r = classify(OWN_URL, final, FULL, "2207", OWN_POLICY)
    assert r["verdict"] == "check"
    assert "wall" in r["why"]


def test_short_body_with_captcha_marker_is_a_wall():
    r = classify(OWN_URL, OWN_URL, "Just a moment... checking your browser (captcha)", "2207", OWN_POLICY)
    assert r["verdict"] == "check"
    assert "wall" in r["why"]


def test_login_word_on_a_full_page_is_not_a_wall():
    body = "Resident Login | " + FULL
    assert detect_wall(OWN_URL, body, "own_page") is None
    r = classify(OWN_URL, OWN_URL, body, "2207", OWN_POLICY)
    assert r["verdict"] == "gone"


def test_gated_kind_without_token_is_check():
    r = classify("https://x.securecafe.com/onlineleasing/p/rentaloptions.aspx",
                 "https://x.securecafe.com/onlineleasing/p/rentaloptions.aspx", FULL, "2207",
                 {"kind": "gated", "live_evidence": True, "gone_evidence": False})
    assert r["verdict"] == "check"


def test_gated_kind_with_token_is_live_without_price():
    body = FULL + "\nApt 2207 $4,380\n"
    r = classify("https://x.securecafe.com/p.aspx", "https://x.securecafe.com/p.aspx", body, "2207",
                 {"kind": "gated", "live_evidence": True, "gone_evidence": False})
    assert r["verdict"] == "live"
    assert r["price"] is None


def test_securecafe_host_without_policy_is_treated_as_gated():
    url = "https://x.securecafe.com/onlineleasing/p/availableunits.aspx"
    r = classify(url, url, FULL, "2207")
    assert r["verdict"] == "check"
    assert "securecafe" in r["why"]


def test_empty_shell_is_check_never_gone():
    body = "Unit unavailable"
    assert len(body) < SHELL_MIN_CHARS
    r = classify(OWN_URL, OWN_URL, body, "2207", OWN_POLICY)
    assert r["verdict"] == "check"
    assert "shell" in r["why"]


# --------------------------------------------------------------------------
# gone: only from a unit-specific page that rendered fully
# --------------------------------------------------------------------------


def test_own_page_that_omits_the_unit_is_gone():
    r = classify(OWN_URL, OWN_URL, FULL, "2207", OWN_POLICY)
    assert r["verdict"] == "gone"
    assert r["token_found"] is False
    assert live_from_verdict(r["verdict"]) is False


def test_own_page_without_gone_evidence_is_check():
    pol = dict(OWN_POLICY, gone_evidence=False)
    r = classify(OWN_URL, OWN_URL, FULL, "2207", pol)
    assert r["verdict"] == "check"
    assert "lacks gone evidence" in r["why"]


def test_complete_table_that_omits_the_unit_is_gone():
    body = _table_body("1704  $5,100  Available Now", "2612  $4,700  Available Sep 1")
    r = classify(INDEX_URL, INDEX_URL, body, "2207", TABLE_POLICY)
    assert r["verdict"] == "gone"
    assert "complete table" in r["why"]


def test_table_not_declared_complete_cannot_prove_gone():
    pol = dict(TABLE_POLICY)
    del pol["complete"]
    body = _table_body("1704  $5,100")
    r = classify(INDEX_URL, INDEX_URL, body, "2207", pol)
    assert r["verdict"] == "check"
    assert "not declared complete" in r["why"]


def test_table_with_pagination_marker_cannot_prove_gone():
    body = _table_body("1704  $5,100") + "\nLoad more\n"
    r = classify(INDEX_URL, INDEX_URL, body, "2207", TABLE_POLICY)
    assert r["verdict"] == "check"
    assert "pagination" in r["why"]


def test_untrusted_surface_is_never_evidence_either_way():
    url = "https://operator.test/inventory/unit/?id=9999"
    present = classify(url, url, FULL + "\nUnit 9999 $4,500 Available Now", "9999", UNTRUSTED_POLICY)
    absent = classify(url, url, FULL, "9999", UNTRUSTED_POLICY)
    assert present["verdict"] == "check"
    assert absent["verdict"] == "check"
    assert present["price"] is None
    assert "untrusted" in present["why"]


def test_label_row_is_check_even_on_own_page():
    r = classify(OWN_URL, OWN_URL, FULL, "2BR-3", OWN_POLICY)
    assert r["verdict"] == "check"
    assert "label row" in r["why"]


def test_prior_gone_with_token_present_is_review_not_live():
    body = FULL + "\nUnit 2207 $4,380\n"
    r = classify(OWN_URL, OWN_URL, body, "2207", OWN_POLICY, prior="gone")
    assert r["verdict"] == "check"
    assert r["review"] is True
    assert r["token_found"] is True
    assert live_from_verdict(r["verdict"]) is None


# --------------------------------------------------------------------------
# prices: own page or own table row only, with the layer detected
# --------------------------------------------------------------------------


def test_own_page_price_is_admissible_with_layer():
    body = FULL + "\nResidence 2207\nBase rent $4,380 / month\n"
    r = classify(OWN_URL, OWN_URL, body, "2207", OWN_POLICY)
    assert r["verdict"] == "live"
    assert r["price"] == 4380
    assert r["price_layer"] == "base"
    assert r["price_trust"] == "ok"


def test_table_price_comes_from_the_units_own_row_not_a_neighbour():
    body = _table_body("1704  $5,100  Available Now", "2207  $4,380  Available Sep 2", "2612  $4,700")
    r = classify(INDEX_URL, INDEX_URL, body, "2207", TABLE_POLICY)
    assert r["verdict"] == "live"
    assert r["price"] == 4380
    assert r["price_layer"] == "base"  # declared by the policy


def test_net_effective_asterisk_is_detected_as_net_layer():
    body = _table_body("2207  $4,500*  net effective  Available Now")
    r = classify(INDEX_URL, INDEX_URL, body, "2207", TABLE_POLICY)
    assert r["price"] == 4500
    assert r["price_layer"] == "net"


def test_price_layer_detection():
    assert price_layer("$4,500*") == "net"
    assert price_layer("Net Effective $4,500") == "net"
    assert price_layer("Base Rent $4,380") == "base"
    assert price_layer("Total monthly $5,120") == "total"
    assert price_layer("$4,380", {"price_layer": "base"}) == "base"
    assert price_layer("$4,380") == "unknown"


def test_price_evidence_false_blocks_the_price_even_on_own_page():
    pol = dict(OWN_POLICY, price_evidence=False)
    body = FULL + "\nUnit 2207 $4,380\n"
    r = classify(OWN_URL, OWN_URL, body, "2207", pol)
    assert r["verdict"] == "live"
    assert r["price"] is None
    assert r["price_trust"] == "none"


# --------------------------------------------------------------------------
# shared-shell rule across a batch
# --------------------------------------------------------------------------


def _gone_row(key: str, host: str, body: str) -> dict:
    r = classify("https://%s/floorplans/unit-%s" % (host, key), "https://%s/floorplans/unit-%s" % (host, key),
                 body, key, OWN_POLICY)
    r["key"] = key
    r["final_url"] = "https://%s/floorplans/unit-%s" % (host, key)
    r["live"] = live_from_verdict(r["verdict"])
    return r


def test_identical_bodies_across_three_units_of_one_host_are_a_shell():
    rows = [_gone_row(k, "operator.test", FULL) for k in ("101", "202", "303")]
    assert all(r["verdict"] == "gone" for r in rows)
    apply_shell_rule(rows)
    assert all(r["verdict"] == "check" and r["live"] is None for r in rows)
    assert all("shell" in r["why"] for r in rows)


def test_shell_rule_needs_the_shared_minimum_and_one_host():
    rows = [_gone_row(k, "operator.test", FULL) for k in ("101", "202")]
    rows.append(_gone_row("303", "other.test", FULL))
    apply_shell_rule(rows)
    assert SHELL_SHARED_MIN == 3
    assert all(r["verdict"] == "gone" for r in rows)


def test_shell_rule_leaves_distinct_bodies_alone():
    # Distinct suffixes that never contain a unit token, so each row is a
    # genuine gone before the shell rule runs.
    rows = [_gone_row(k, "operator.test", FULL + " copy " + s) for k, s in (("101", "A"), ("202", "B"), ("303", "C"))]
    assert all(r["verdict"] == "gone" for r in rows)
    apply_shell_rule(rows)
    assert all(r["verdict"] == "gone" for r in rows)


# --------------------------------------------------------------------------
# policy loading and matching
# --------------------------------------------------------------------------


def test_policy_first_match_wins_so_specific_before_generic():
    sources = [UNTRUSTED_POLICY, {"match": "/inventory", "kind": "table", "complete": True}]
    assert policy_for("https://op.test/inventory/unit/?id=1", sources)["kind"] == "untrusted"
    assert policy_for("https://op.test/inventory/", sources)["kind"] == "table"
    assert policy_for("https://elsewhere.test/", sources) == {}


def test_load_sources_json_accepts_list_or_sources_dict(tmp_path):
    as_list = tmp_path / "a.json"
    as_list.write_text(json.dumps([OWN_POLICY]))
    as_dict = tmp_path / "b.json"
    as_dict.write_text(json.dumps({"sources": [OWN_POLICY, TABLE_POLICY]}))
    assert load_sources(as_list)[0]["kind"] == "own_page"
    assert [s["kind"] for s in load_sources(as_dict)] == ["own_page", "table"]


def test_load_sources_yaml(tmp_path):
    yaml = pytest.importorskip("yaml")
    p = tmp_path / "sources.yml"
    p.write_text(yaml.safe_dump({"sources": [TABLE_POLICY]}))
    assert load_sources(p)[0]["complete"] is True


def test_load_sources_rejects_non_list(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"sources": "nope"}))
    with pytest.raises(ValueError):
        load_sources(p)


def test_shell_rule_ignores_digits_such_as_request_ids():
    # A shell that echoes the requested unit number or a request id must
    # still group as one shell.
    rows = [_gone_row(k, "operator.test", FULL + " request id %s" % rid)
            for k, rid in (("101", "88231"), ("202", "88232"), ("303", "88233"))]
    assert all(r["verdict"] == "gone" for r in rows)
    apply_shell_rule(rows)
    assert all(r["verdict"] == "check" for r in rows)


def test_own_page_price_far_from_token_is_not_read():
    # A deposit or fee figure elsewhere on the unit's own page is not the
    # unit's price: no page-wide fallback.
    body = "Security deposit $1,500\n" + FULL + "\nResidence 2207 is available\n" + FULL
    r = classify(OWN_URL, OWN_URL, body, "2207", OWN_POLICY)
    assert r["verdict"] == "live"
    assert r["price"] is None
    assert r["price_trust"] == "none"
