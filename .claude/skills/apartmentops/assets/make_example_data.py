#!/usr/bin/env python3
"""Seeded generator for the synthetic dataset in example-dashboard.html.

Everything the example dashboard shows about buildings, units, prices,
grades, commutes and resident reports is invented here from one fixed seed
(random.Random(20260718)), so the output is byte-for-byte reproducible and
no row describes a real listing or a real person's shortlist. Only the
geography (and the footer's transit fare table) is real: the generator
reads the transit stations (var STN) and the map projection from the HTML
and scatters each fictional building 250 to 700 metres from one station,
then labels it by that station.

Usage (from anywhere, stdlib only):

    python make_example_data.py              # JSON {"B", "LINKS", "LBL"} to stdout
    python make_example_data.py --js         # the three "var X = ...;" lines
    python make_example_data.py --splice     # rewrite those lines in the HTML

The data is shaped for the render code in example-dashboard.html and
deliberately exercises every optional feature: gone and recheck rows,
net-effective prices (with and without a gross figure), every exposure
badge shape including a unit with no exp at all, parking with and without
a published rate, a building with no parking source, gate OK / FAIL / ?,
mandatory and optional fees, a building with no second commute, units that
fit / nearly fit / miss / do not state a date for the move-in window, and
units with no sqft or no floor on file.

Rents end in 00, 30, 80 or 90 so no generated figure coincides with any
rent that appeared in the pre-anonymization version of this file (that
file's rents never used those endings), and coordinates carry five
decimals with a non-zero last digit so none equals a four-decimal
coordinate from it. Neither set is embedded here on purpose.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import pathlib
import random
import re
import sys

SEED = 20260718
HTML = pathlib.Path(__file__).with_name("example-dashboard.html")

# Building names and ids are invented; none is meant to resemble a real
# building anywhere.
NAMES = [
    "Copperline Tower", "Larkspur Point", "Glasswing Commons", "Quillon Place",
    "The Fennmore", "Saltmarsh Tower", "Ironbark Yards", "Sundial Tower",
    "The Wrenfield", "Kestrel Court", "Bellwether Point", "Ashgrove Terrace",
    "Oxbow Lofts", "Marlin Cross", "Halcyon Row",
]
# Home station per building (STN keys), shuffled by the seed below.
HOME_STATIONS = [
    "jsq", "jsq", "jsq", "newport", "newport", "grove", "grove", "hoboken",
    "hoboken", "vernon", "vernon", "f21", "astbl", "astbl", "s46",
]
# Which STN keys are PATH stations (leg type "path"); the rest are subway.
PATH_STATIONS = {"newport", "jsq", "hoboken", "grove", "p14", "p23"}
# Synthetic ride minutes from each home station to the Manhattan station
# the route ends at, per office. Office A sits in Midtown, Office B near
# Union Square, so PATH riders alight at 23 St / 14 St and subway riders at
# Grand Central, 23 St F/M or 23 St R/W. A building may walk to a second
# station in its cluster (vernon <-> f21) when that line serves the office.
ROUTES = {
    "newport": {"a": ("p23", 12), "b": ("p14", 10)},
    "jsq":     {"a": ("p23", 21), "b": ("p14", 19)},
    "hoboken": {"a": ("p23", 14), "b": ("p14", 12)},
    "grove":   {"a": ("p23", 17), "b": ("p14", 15)},
    "vernon":  {"a": ("gct", 8), "b": ("m23", 12, "f21")},
    "f21":     {"a": ("gct", 8, "vernon"), "b": ("m23", 12)},
    "astbl":   {"a": ("rw23", 24), "b": ("rw23", 24)},
    "s46":     {"a": ("m23", 26), "b": ("rw23", 28)},
}
# Walk minutes from the last station to each office anchor.
OFFICE_WALK = {"a": {"p23": 15, "gct": 14, "rw23": 15, "m23": 16},
               "b": {"p14": 11, "m23": 14, "rw23": 13}}
MONTHLY_COST = {"path": 130, "subway": 126}
GRADES = ["A-", "B+", "B", "C+", "C"]  # the dashboard's safety sorter ranks exactly these
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
PRICE_ENDINGS = (0, 30, 80, 90)
RENT_MIN, RENT_MAX = 3900, 6400
SQFT_MIN, SQFT_MAX = 780, 1320
JITTER_MIN_M, JITTER_MAX_M = 250, 700
MIN_SEPARATION_M = 200

ESSENTIALS = [
    ("Example Grocery", (2, 8)), ("Example Pharmacy", (2, 7)), ("Example Gym", (1, 6)),
    ("Example Coffee", (1, 4)), ("Example Park", (3, 9)), ("Example Dry Cleaner", (2, 7)),
    ("Example Hardware", (4, 10)), ("Example Bakery", (2, 6)), ("Example Daycare", (4, 9)),
]
VIBES = [
    "Lobby is quiet after 8 pm.", "Elevators run slow at the morning peak.",
    "Package room is staffed on weekdays.", "Gym is small but rarely crowded.",
    "Street noise reaches the low floors on the avenue side.", "Roof deck closes at 10 pm.",
    "Water pressure is strong on the upper floors.", "Trash chutes on every floor.",
    "Front desk holds dry cleaning.", "Nearby construction is expected through 2027.",
    "Laundry is in-unit on every line.", "Bike room fills up by spring.",
]
FEE_SETS = [
    [{"label": "Amenity fee", "monthly": 75, "mandatory": True},
     {"label": "Trash", "monthly": 25, "mandatory": True}],
    [{"label": "Utility admin", "monthly": 15, "mandatory": True},
     {"label": "Bike storage", "monthly": 20, "mandatory": False}],
    [{"label": "Amenity fee", "monthly": 100, "mandatory": True}],
]
# Parking profiles; the hard gate in this example is derived from parking.
PARKING_PROFILES = (
    ["garage_cost"] * 5 + ["garage_nocost"] * 3 + ["valet"] * 2
    + ["none"] * 2 + ["unknown"] * 2 + ["absent"]
)


# ---- reading the HTML for geography and dates -----------------------------

def read_html(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def parse_stations(html: str) -> dict[str, dict]:
    """Return {key: {"n": name, "ll": [lat, lon]}} from the var STN block."""
    block = re.search(r"var STN = \{(.*?)\n\s*\};", html, re.S)
    if not block:
        raise SystemExit("var STN block not found in the HTML")
    stn: dict[str, dict] = {}
    for m in re.finditer(r'(\w+):\s*\{ n: "([^"]*)", ll: \[([-\d.]+),\s*([-\d.]+)\] \}', block.group(1)):
        stn[m.group(1)] = {"n": m.group(2), "ll": [float(m.group(3)), float(m.group(4))]}
    if not stn:
        raise SystemExit("no stations parsed from var STN")
    return stn


def parse_var(html: str, name: str, pattern: str = r'"([^"]+)"') -> str:
    m = re.search(r"var %s = %s;" % (re.escape(name), pattern), html)
    if not m:
        raise SystemExit("var %s not found in the HTML" % name)
    return m.group(1)


def parse_projection(html: str) -> dict[str, float]:
    m = re.search(r"var LON0 = ([-\d.]+), LON1 = ([-\d.]+), LAT0 = ([-\d.]+), LAT1 = ([-\d.]+);", html)
    w = re.search(r"var W = (\d+), H = (\d+);", html)
    if not m or not w:
        raise SystemExit("map projection constants not found in the HTML")
    return {"lon0": float(m.group(1)), "lon1": float(m.group(2)), "lat0": float(m.group(3)),
            "lat1": float(m.group(4)), "w": float(w.group(1)), "h": float(w.group(2))}


# ---- small helpers ---------------------------------------------------------

def round5(x: float) -> float:
    """Five decimals with a non-zero last digit (never a four-decimal value)."""
    v = int(round(x * 1e5))
    if v % 10 == 0:
        v += 1
    return v / 1e5


def offset(ll: list[float], dist_m: float, bearing_deg: float) -> list[float]:
    lat, lon = ll
    b = math.radians(bearing_deg)
    dlat = dist_m * math.cos(b) / 111320.0
    dlon = dist_m * math.sin(b) / (111320.0 * math.cos(math.radians(lat)))
    return [round5(lat + dlat), round5(lon + dlon)]


def dist_m(a: list[float], b: list[float]) -> float:
    lat = math.radians((a[0] + b[0]) / 2)
    dy = (a[0] - b[0]) * 111320.0
    dx = (a[1] - b[1]) * 111320.0 * math.cos(lat)
    return math.hypot(dx, dy)


def slug(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-")


def snap_price(x: float) -> int:
    """Nearest integer whose last two digits are one of PRICE_ENDINGS."""
    base = int(x // 100) * 100
    cands = [base + e for e in PRICE_ENDINGS] + [base + 100 + e for e in PRICE_ENDINGS] + [base - 100 + e for e in PRICE_ENDINGS]
    return min(cands, key=lambda c: (abs(c - x), c))


def mon_day(d: dt.date) -> str:
    return "%s %d" % (MONTHS[d.month - 1], d.day)


def iso(d: dt.date) -> str:
    return d.isoformat()


# ---- the generator ---------------------------------------------------------

def generate(html_path: pathlib.Path = HTML) -> dict:
    html = read_html(html_path)
    stn = parse_stations(html)
    proj = parse_projection(html)
    run_date = dt.date.fromisoformat(parse_var(html, "RUN_DATE"))
    target = dt.date.fromisoformat(parse_var(html, "MOVE_TARGET"))
    for k in HOME_STATIONS:
        if k not in stn:
            raise SystemExit("home station %r is not in var STN" % k)
    rng = random.Random(SEED)

    stations = list(HOME_STATIONS)
    rng.shuffle(stations)
    # short id = the name's first distinctive word ("The Fennmore" -> "fennmore")
    ids = [slug(n.replace("The ", "").split()[0]) for n in NAMES]
    assert len(set(ids)) == len(ids)

    # -- placement: one bearing slot per building within its station cluster,
    #    re-rolled until the building is 250-700 m from its own station, its
    #    nearest station IS its own station, and it is clear of every other
    #    building; and it lies inside the map projection.
    cluster_n = {k: stations.count(k) for k in set(stations)}
    cluster_i: dict[str, int] = {}
    placed: list[list[float]] = []
    lls: list[list[float]] = []
    for k in stations:
        i = cluster_i.get(k, 0)
        cluster_i[k] = i + 1
        base = rng.uniform(0, 360)
        for _attempt in range(200):
            bearing = base + i * 360.0 / cluster_n[k] + rng.uniform(-25, 25)
            ll = offset(stn[k]["ll"], rng.uniform(JITTER_MIN_M + 20, JITTER_MAX_M - 20), bearing)
            d_home = dist_m(ll, stn[k]["ll"])
            nearest = min(stn, key=lambda s: dist_m(ll, stn[s]["ll"]))
            inside = proj["lon0"] < ll[1] < proj["lon1"] and proj["lat0"] < ll[0] < proj["lat1"]
            clear = all(dist_m(ll, q) >= MIN_SEPARATION_M for q in placed)
            if JITTER_MIN_M <= d_home <= JITTER_MAX_M and nearest == k and inside and clear:
                break
        else:
            raise SystemExit("could not place a building near %s" % k)
        placed.append(ll)
        lls.append(ll)

    # -- per-building feature decks (shuffled so coverage is guaranteed but
    #    the assignment looks natural)
    parking_deck = list(PARKING_PROFILES)
    rng.shuffle(parking_deck)
    fee_deck = [0, 1, 2] + [None] * (len(NAMES) - 3)
    rng.shuffle(fee_deck)
    no_commute2 = rng.randrange(len(NAMES))
    no_grating = (no_commute2 + 5) % len(NAMES)
    null_count = (no_commute2 + 9) % len(NAMES)

    # -- per-unit decks
    counts = []
    while True:
        counts = [rng.choice([1, 2, 2, 3, 3, 4]) for _ in NAMES]
        if 36 <= sum(counts) <= 44:
            break
    n_units = sum(counts)
    status_deck = ["gone"] * 4 + ["check"] * 2 + ["live"] * (n_units - 6)
    rng.shuffle(status_deck)
    n_live = n_units - 4
    avail_required = ["fits", "fits", "now", "near_early", "near_late", "off_early", "off_late", "unknown"]
    avail_pool = ["fits", "fits", "fits", "now", "near_early", "near_late", "off_late", "unknown"]
    avail_deck = avail_required + [rng.choice(avail_pool) for _ in range(n_live - len(avail_required))]
    rng.shuffle(avail_deck)
    exp_required = ["south", "south", "maybe", "unk", "unk", "notsouth_dir", "notsouth_nodir", "missing"]
    exp_pool = ["south", "maybe", "unk", "notsouth_dir", "notsouth_dir", "notsouth_dir", "notsouth_nodir"]
    exp_deck = exp_required + [rng.choice(exp_pool) for _ in range(n_units - len(exp_required))]
    rng.shuffle(exp_deck)
    net_deck = ["gross_net"] * 5 + ["net_only"] * 3 + ["gross"] * (n_units - 8)
    rng.shuffle(net_deck)
    sqft_missing = set(rng.sample(range(n_units), 3))
    ph_slots = rng.sample(range(len(NAMES)), 2)  # two buildings get a penthouse row

    used_rents: set[int] = set()
    used_unit_ids: set[str] = set()
    used_addr_nums: set[int] = set()

    def new_rent(lo: int = RENT_MIN, hi: int = RENT_MAX) -> int:
        for _ in range(500):
            r = snap_price(rng.uniform(lo, hi))
            if lo <= r <= hi and r not in used_rents:
                used_rents.add(r)
                return r
        raise SystemExit("ran out of distinct rents")

    def avail_for(cat: str) -> str:
        ws = target - dt.timedelta(days=14)
        we = target + dt.timedelta(days=14)
        if cat == "now":
            return rng.choice(["Now", "Listed now", "Now (repriced)"])
        if cat == "fits":
            d = ws + dt.timedelta(days=rng.randint(0, 28))
            return mon_day(d) + rng.choice(["", "", " (NEW this week)"])
        if cat == "near_early":
            d = ws - dt.timedelta(days=rng.randint(3, 40))
            return mon_day(d)
        if cat == "near_late":
            d = we + dt.timedelta(days=rng.randint(2, 40))
            return mon_day(d)
        if cat == "off_early":
            # after RUN_DATE - 60 days, so the parser keeps it in this year
            d = ws - dt.timedelta(days=rng.randint(45, 58))
            assert d > run_date - dt.timedelta(days=60)
            return mon_day(d)
        if cat == "off_late":
            d = we + dt.timedelta(days=rng.randint(45, 100))
            return mon_day(d) + rng.choice(["", " (by term)"])
        return rng.choice(["Date on request", "Waitlist, no date given"])

    # exp.dir is always one of contracts.md's eight compass points (N, NE,
    # E, SE, S, SW, W, NW) or None; never a 16-point value such as SSW.
    def exp_for(shape: str) -> dict | None:
        if shape == "south":
            return {"dir": rng.choice(["S", "SW", "SE"]), "south": True, "conf": "HIGH",
                    "src": "Floor-plan key plan (example data)"}
        if shape == "maybe":
            return {"dir": rng.choice(["S", "SE", "SW"]), "south": None, "conf": "LOW",
                    "src": "Listing statement only, no key plan on file (example data)"}
        if shape == "unk":
            return {"dir": None, "south": None, "conf": "UNK", "src": "Unresolved: no floor plan on file"}
        if shape == "notsouth_dir":
            conf = rng.choice(["HIGH", "HIGH", "MED"])
            return {"dir": rng.choice(["N", "NE", "E", "W", "NW"]), "south": False, "conf": conf,
                    "src": "Floor-plan key plan (example data)" if conf == "HIGH" else "Footprint bearing only (example data)"}
        if shape == "notsouth_nodir":
            return {"dir": None, "south": False, "conf": "HIGH", "src": "Floor-plan verified not south (example data)"}
        return None

    def verified_for(status: str) -> str:
        if status == "live":
            back = rng.choice([0, 0, 0, 0, 1, 2, 5])
        elif status == "check":
            back = rng.randint(7, 28)
        else:
            back = rng.randint(1, 20)
        return iso(run_date - dt.timedelta(days=back))

    B: list[dict] = []
    LINKS: dict[str, dict] = {}
    LBL: dict[str, dict] = {}
    unit_i = 0
    for bi, (name, bid, k, ll) in enumerate(zip(NAMES, ids, stations, lls)):
        area = "Near " + stn[k]["n"]
        area_slug = slug(area)
        floors = rng.randint(12, 58)
        while True:
            num = rng.randint(100, 999)
            if num not in used_addr_nums:
                used_addr_nums.add(num)
                break
        addr = "%d Example %s, %s" % (num, rng.choice(["St", "Ave"]), area)

        def commute(which: str) -> dict:
            spec = ROUTES[k][which]
            end, ride = spec[0], spec[1]
            board = spec[2] if len(spec) > 2 else k
            walk1 = rng.randint(3, 12) if board == k else rng.randint(14, 18)
            walk2 = OFFICE_WALK[which][end]
            wait = 4
            total = walk1 + wait + ride + walk2
            kind = "path" if board in PATH_STATIONS else "subway"
            line = "PATH" if kind == "path" else "Subway"
            office = "Office A" if which == "a" else "Office B"
            end_name = stn[end]["n"]
            if kind == "path" and end_name.endswith(" PATH"):
                end_name = end_name[:-5]  # "PATH to 23 St", not "PATH to 23 St PATH"
            route = "Walk %d min to %s, %s to %s (%d min), walk %d min to %s" % (
                walk1, stn[board]["n"], line, end_name, ride, walk2, office)
            legs = [["walk", "self", board], [kind, board, end], ["walk", end, "office" if which == "a" else "office2"]]
            return {"min": total, "range": "%d-%d" % (total - 4, total + 7), "cost": MONTHLY_COST[kind],
                    "mode": kind, "route": route, "legs": legs}

        b: dict = {
            "id": bid, "name": name, "addr": addr, "ll": ll, "area": area,
            "safety": None, "clean": None, "commute": commute("a"),
        }
        if bi != no_commute2:
            b["commute2"] = commute("b")
        sg, cg = rng.choice(GRADES), rng.choice(GRADES)
        b["safety"] = {"g": sg, "c": sg[0]}
        b["clean"] = {"g": cg, "c": cg[0]}

        # units
        units = []
        for j in range(counts[bi]):
            status = status_deck[unit_i]
            if bi in ph_slots and j == counts[bi] - 1:
                # one penthouse per slot: PH2 with no published floor, PH3 on the top floor
                uid = "PH%d" % (2 + ph_slots.index(bi))
                fl = None if bi == ph_slots[0] else floors
            else:
                while True:
                    fl = rng.randint(3, floors)
                    uid = "%02d%02d" % (fl, rng.randint(1, 12))
                    if uid not in used_unit_ids:
                        break
            assert uid not in used_unit_ids
            used_unit_ids.add(uid)
            u: dict = {"u": uid, "fl": fl}
            price_mode = net_deck[unit_i]
            if price_mode == "gross":
                u["rent"] = new_rent()
            elif price_mode == "gross_net":
                rent = new_rent(4700, RENT_MAX)
                free = rng.choice([1, 2])
                net = snap_price(rent * (12 - free) / 12.0)
                assert RENT_MIN <= net < rent
                used_rents.add(net)
                u["rent"], u["net"] = rent, net
            else:
                u["rent"] = None
                u["net"] = new_rent()
            if unit_i not in sqft_missing:
                u["sqft"] = rng.randint(SQFT_MIN, SQFT_MAX)
            if status == "gone":
                u["avail"] = rng.choice(["GONE", "GONE (leased %s)" % mon_day(run_date - dt.timedelta(days=rng.randint(2, 12)))])
                u["live"] = False
            else:
                u["avail"] = avail_for(avail_deck.pop())
                # live is the prior boolean verdict (never a string, per
                # contracts.md); a recheck row keeps it and carries the
                # latest three-state verdict separately.
                u["live"] = True
                if status == "check":
                    u["verdict"] = "check"
            u["link"] = "https://example.com/%s/units/%s" % (bid, uid)
            e = exp_for(exp_deck[unit_i])
            if e is not None:
                u["exp"] = e
            u["verified_at"] = verified_for(status)
            units.append(u)
            unit_i += 1
        b["units"] = units

        # resident report and essentials
        ess = rng.sample(ESSENTIALS, rng.randint(3, 4))
        b["ess"] = [[label, "%d min" % rng.randint(lo, hi)] for label, (lo, hi) in ess]
        b["vibe"] = "Resident report (example): %s %s Built %d, %d floors." % (
            rng.choice(VIBES), rng.choice(VIBES), rng.randint(2016, 2025), floors)

        # fees, google rating, parking, gate
        fi = fee_deck[bi]
        b["fees"] = [dict(f, source="https://example.com/%s/fees" % bid) for f in FEE_SETS[fi]] if fi is not None else []
        if bi == no_grating:
            b["grating"] = None
        else:
            b["grating"] = {"stars": round(rng.uniform(3.4, 4.8), 1),
                            "count": None if bi == null_count else rng.randint(40, 1500),
                            "url": "https://example.com/%s/rating" % bid}
        prof = parking_deck[bi]
        purl = "https://example.com/%s/parking" % bid
        if prof == "garage_cost":
            b["parking"] = {"avail": "garage", "cost": rng.choice([225, 250, 275, 300, 325]),
                            "note": "On-site garage with a published monthly rate (example data)", "url": purl}
            b["gate"] = {"ok": True, "why": "On-site garage parking per the building's parking page (example data)"}
        elif prof == "garage_nocost":
            b["parking"] = {"avail": "garage", "cost": None,
                            "note": "On-site garage, monthly rate not published (example data)", "url": purl}
            b["gate"] = {"ok": True, "why": "On-site garage parking per the building's parking page (example data)"}
        elif prof == "valet":
            b["parking"] = {"avail": "valet", "cost": None,
                            "note": "Valet parking listed on the amenities page, rate not published (example data)", "url": purl}
            b["gate"] = {"ok": True, "why": "On-site valet parking per the building's amenities page (example data)"}
        elif prof == "none":
            b["parking"] = {"avail": "none", "cost": None,
                            "note": "No on-site parking per the building's amenities page (example data)", "url": purl}
            b["gate"] = {"ok": False, "why": "No on-site parking per the building's amenities page (example data)"}
        elif prof == "unknown":
            b["parking"] = {"avail": "unknown", "cost": None,
                            "note": "Parking not stated by any source on file (example data)", "url": purl}
            b["gate"] = {"ok": None, "why": "Parking not verified: no source on file (example data)"}
        # "absent": no parking key and no gate key at all

        B.append(b)
        LINKS[bid] = {"site": "https://example.com/%s/availability" % bid,
                      "safety": "https://example.com/sources/%s-safety" % area_slug,
                      "clean": "https://example.com/sources/%s-cleanliness" % area_slug}

    assert unit_i == n_units and not avail_deck
    LBL = _labels(B, stations, stn, proj, parse_offices(html))
    _check(B, LINKS, LBL, stn, proj, run_date)
    return {"B": B, "LINKS": LINKS, "LBL": LBL}


def parse_offices(html: str) -> list[list[float]]:
    return [[float(m.group(1)), float(m.group(2))]
            for m in re.finditer(r'var OFFICE2? = \{ n: "[^"]*", ll: \[([-\d.]+), ([-\d.]+)\] \};', html)]


def _labels(B: list[dict], stations: list[str], stn: dict, proj: dict, offices: list[list[float]]) -> dict[str, dict]:
    """Map label nudges {dx, dy, anchor} per building.

    Greedy placement in the map's pixel space: a label prefers to point away
    from its home station (so a cluster fans out), must stay inside the
    viewBox, and must not overlap another label, a building marker or an
    office label. Widths are estimated from the rendered text length.
    """
    W, H = proj["w"], proj["h"]

    def px(ll: list[float]) -> tuple[float, float]:
        return ((ll[1] - proj["lon0"]) / (proj["lon1"] - proj["lon0"]) * W,
                (proj["lat1"] - ll[0]) / (proj["lat1"] - proj["lat0"]) * H)

    def hits(box: tuple, boxes: list[tuple]) -> bool:
        x0, y0, x1, y1 = box
        return any(not (x1 < bx0 or x0 > bx1 or y1 < by0 or y0 > by1) for bx0, by0, bx1, by1 in boxes)

    boxes: list[tuple] = []
    for b in B:  # marker discs
        x, y = px(b["ll"])
        boxes.append((x - 9, y - 9, x + 9, y + 9))
    for ll in offices:  # "OFFICE A (EXAMPLE)" text to the right of the marker
        x, y = px(ll)
        boxes.append((x - 9, y - 20, x + 12 + 6.4 * 18, y + 8))

    LBL: dict[str, dict] = {}
    for b, k in zip(B, stations):
        x, y = px(b["ll"])
        text_w = 5.6 * (len(b["name"]) + 8)  # name + "  NN/NNm" at 10.5px
        east = b["ll"][1] >= stn[k]["ll"][1]
        north = b["ll"][0] >= stn[k]["ll"][0]
        sides = ("start", "end") if east else ("end", "start")
        cands = [(sides[0], -8 if north else 14)]
        cands += [(a, d) for d in (-8, 14, -26, 26, 2) for a in sides if (a, d) not in cands]
        chosen = None
        for anchor, dy in cands:
            dx = 13 if anchor == "start" else -12
            x0 = x + dx if anchor == "start" else x + dx - text_w
            box = (x0, y + dy - 10, x0 + text_w, y + dy + 3)
            if box[0] < 4 or box[2] > W - 4 or box[1] < 4 or box[3] > H - 4 or hits(box, boxes):
                continue
            chosen = (anchor, dx, dy, box)
            break
        if chosen is None:  # nothing clean; fall back to the preferred side
            anchor, dy = cands[0]
            dx = 13 if anchor == "start" else -12
            x0 = x + dx if anchor == "start" else x + dx - text_w
            chosen = (anchor, dx, dy, (x0, y + dy - 10, x0 + text_w, y + dy + 3))
        anchor, dx, dy, box = chosen
        boxes.append(box)
        LBL[b["id"]] = {"dx": dx, "dy": dy, "anchor": anchor}
    return LBL


def _check(B: list[dict], LINKS: dict, LBL: dict, stn: dict, proj: dict, run_date: dt.date) -> None:
    """Coverage and shape asserts so a broken edit fails here, not in a browser."""
    units = [u for b in B for u in b["units"]]
    assert 14 <= len(B) <= 16 and 36 <= len(units) <= 44
    assert len({b["id"] for b in B}) == len(B)
    assert len({u["u"] for u in units}) == len(units)
    assert set(LINKS) == set(LBL) == {b["id"] for b in B}
    for b in B:
        assert JITTER_MIN_M <= min(dist_m(b["ll"], s["ll"]) for s in stn.values()) <= JITTER_MAX_M
        assert proj["lon0"] < b["ll"][1] < proj["lon1"] and proj["lat0"] < b["ll"][0] < proj["lat1"]
        assert all(int(round(v * 1e5)) % 10 != 0 for v in b["ll"])
        assert b["safety"]["g"] in GRADES and b["clean"]["g"] in GRADES
        for key in ("commute", "commute2"):
            c = b.get(key)
            if c is None:
                continue
            assert c["legs"][0][1] == "self" and c["legs"][-1][2] == ("office" if key == "commute" else "office2")
            for kind, a, z in c["legs"]:
                assert kind in ("walk", "path", "subway")
                for end in (a, z):
                    assert end in stn or end in ("self", "office", "office2"), end
    for u in units:
        for v in (u.get("rent"), u.get("net")):
            if v is not None:
                assert RENT_MIN <= v <= RENT_MAX and v % 100 in PRICE_ENDINGS, v
        if "sqft" in u:
            assert SQFT_MIN <= u["sqft"] <= SQFT_MAX
        d = dt.date.fromisoformat(u["verified_at"])
        assert run_date - dt.timedelta(days=30) <= d <= run_date
        assert u["link"].startswith("https://example.com/")
    assert all(u["live"] is True or u["live"] is False for u in units)
    assert any(u["live"] is False for u in units)
    assert any(u.get("verdict") == "check" for u in units)
    assert all(u["live"] is True for u in units if u.get("verdict") == "check")
    assert all(u["exp"]["dir"] in (None, "N", "NE", "E", "SE", "S", "SW", "W", "NW") for u in units if u.get("exp"))
    assert any(u.get("rent") is None and u.get("net") for u in units)
    assert any(u.get("rent") and u.get("net") for u in units)
    assert any("exp" not in u for u in units)
    assert any(u.get("exp", {}).get("conf") == "UNK" for u in units)
    assert any(u.get("exp", {}).get("south") is True for u in units)
    assert any(u.get("exp", {}).get("south") is None and (u["exp"].get("dir") or "").find("S") >= 0 for u in units if u.get("exp"))
    assert any(u.get("exp", {}).get("south") is False and u["exp"].get("dir") is None for u in units if u.get("exp"))
    assert any(u.get("fl") is None for u in units) and any("sqft" not in u for u in units)
    assert any("commute2" not in b for b in B)
    assert any(b.get("gate", {}) and b["gate"]["ok"] is False for b in B)
    assert any(b.get("gate") and b["gate"]["ok"] is None for b in B)
    assert any("gate" not in b for b in B) and any("parking" not in b for b in B)
    assert any(b.get("parking", {}).get("avail") == "garage" and b["parking"]["cost"] for b in B)
    assert any(b.get("parking", {}).get("avail") == "garage" and b["parking"]["cost"] is None for b in B)
    assert any(b.get("parking", {}).get("avail") == "valet" for b in B)
    assert any(b.get("parking", {}).get("avail") == "unknown" for b in B)
    assert sum(1 for b in B if b["fees"]) >= 2
    assert any(f["mandatory"] is False for b in B for f in b["fees"])
    assert any(b["grating"] is None for b in B) and any(b["grating"] and b["grating"]["count"] is None for b in B)


# ---- output ----------------------------------------------------------------

def compact(obj: object) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True)


def js_lines(data: dict) -> dict[str, str]:
    return {name: "var %s = %s;" % (name, compact(data[name])) for name in ("B", "LINKS", "LBL")}


def splice(html_path: pathlib.Path, data: dict) -> None:
    """Replace the one-line var B / var LINKS / var LBL statements in place."""
    html = read_html(html_path)
    for name, line in js_lines(data).items():
        pat = re.compile(r"^([ \t]*)var %s = [\[{].*[\]}];[ \t]*$" % name, re.M)
        hits = pat.findall(html)
        if len(hits) != 1:
            raise SystemExit("expected exactly one one-line 'var %s = ...;' statement, found %d" % (name, len(hits)))
        html = pat.sub(lambda m: m.group(1) + line, html, count=1)
    html_path.write_text(html, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--html", type=pathlib.Path, default=HTML, help="example-dashboard.html to read STN from (and to splice)")
    ap.add_argument("--js", action="store_true", help="print the three var lines instead of JSON")
    ap.add_argument("--splice", action="store_true", help="rewrite var B / LINKS / LBL inside --html")
    args = ap.parse_args(argv)
    data = generate(args.html)
    if args.splice:
        splice(args.html, data)
        units = sum(len(b["units"]) for b in data["B"])
        print("spliced %d buildings / %d units into %s" % (len(data["B"]), units, args.html))
    elif args.js:
        for line in js_lines(data).values():
            print(line)
    else:
        print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
