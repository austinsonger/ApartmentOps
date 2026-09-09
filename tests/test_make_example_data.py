"""Tests for the example dashboard's synthetic-data generator.

The generator lives in assets/ (not scripts/), so it is loaded by path
rather than through conftest's scripts/ path hook.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re

import pytest

ASSETS = pathlib.Path(__file__).resolve().parent.parent / ".claude" / "skills" / "apartmentops" / "assets"
GENERATOR = ASSETS / "make_example_data.py"
DASHBOARD = ASSETS / "example-dashboard.html"


def _load_generator():
    spec = importlib.util.spec_from_file_location("make_example_data", GENERATOR)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gen():
    return _load_generator()


@pytest.fixture(scope="module")
def data(gen):
    return gen.generate(DASHBOARD)


def _spliced(name: str) -> object:
    html = DASHBOARD.read_text(encoding="utf-8")
    m = re.search(r"^[ \t]*var %s = ([\[{].*[\]}]);[ \t]*$" % name, html, re.M)
    assert m, "var %s not found as a one-line statement" % name
    return json.loads(m.group(1))


def test_output_is_deterministic_and_matches_the_dashboard(gen, data):
    again = gen.generate(DASHBOARD)
    assert json.dumps(again, sort_keys=True) == json.dumps(data, sort_keys=True)
    for name in ("B", "LINKS", "LBL"):
        assert _spliced(name) == data[name], "%s in example-dashboard.html is out of date; rerun make_example_data.py --splice" % name


def test_building_and_unit_ids_are_unique(data):
    ids = [b["id"] for b in data["B"]]
    assert len(set(ids)) == len(ids)
    assert 14 <= len(ids) <= 16
    unit_ids = [u["u"] for b in data["B"] for u in b["units"]]
    assert len(set(unit_ids)) == len(unit_ids)
    assert 36 <= len(unit_ids) <= 44
    assert set(data["LINKS"]) == set(ids) == set(data["LBL"])


def test_every_link_is_on_example_com(data):
    urls = []
    for b in data["B"]:
        urls += [u["link"] for u in b["units"]]
        urls += [f["source"] for f in b.get("fees", [])]
        if b.get("parking"):
            urls.append(b["parking"]["url"])
        if b.get("grating"):
            urls.append(b["grating"]["url"])
    for links in data["LINKS"].values():
        urls += [links["site"], links["safety"], links["clean"]]
    assert urls
    assert all(u.startswith("https://example.com/") for u in urls), [u for u in urls if not u.startswith("https://example.com/")]


def test_commute_legs_end_at_stations_or_anchors(gen, data):
    stn = gen.parse_stations(DASHBOARD.read_text(encoding="utf-8"))
    allowed = set(stn) | {"office", "office2", "self"}
    for b in data["B"]:
        for key, anchor in (("commute", "office"), ("commute2", "office2")):
            c = b.get(key)
            if c is None:
                assert key == "commute2"
                continue
            legs = c["legs"]
            assert legs[0][1] == "self" and legs[-1][2] == anchor
            for kind, start, end in legs:
                assert kind in ("walk", "path", "subway")
                assert start in allowed and end in allowed, (b["id"], key, start, end)
