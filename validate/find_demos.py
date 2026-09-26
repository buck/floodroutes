#!/usr/bin/env python3
"""Find good demo trips between recognizable places (against the local API on :8086).

For every pair of places 4-15 km apart it asks for both routes, then replays the
"ignore advice" simulation the way the map page does (car drives the grey route in
25 m steps; re-plan when > 50 m off the blue route, at most every 320 m of driving,
i.e. 3 s at 8x) and scores the trip for each kind of demo.

Usage: find_demos.py [--top 6]   -> docs/demo_candidates.md
"""
import argparse
import itertools
import math
import sys
from pathlib import Path

import httpx
from pyproj import Transformer
from shapely.geometry import LineString, Point
from shapely.ops import transform

ROOT = Path(__file__).resolve().parent.parent
API = "http://127.0.0.1:8086"
DOC = ROOT / "docs" / "demo_candidates.md"
to_m = Transformer.from_crs(4326, 2278, always_xy=True).transform
FT = 0.3048006096

PLACES = ["Rice University", "Houston Zoo", "William P. Hobby Airport", "901 Bagby St", "Memorial Park",
          "Houston Heights", "Montrose", "University of Houston", "Texas Southern University", "The Galleria",
          "Memorial City", "Kashmere Gardens", "Westchase", "Gulfton", "Sharpstown", "Independence Heights",
          "Bellaire", "West University Place", "Midtown", "EaDo/Stadium", "Second Ward", "Fifth Ward",
          "Sunnyside", "Park Place", "Hermann Park", "4800 N Braeswood Blvd", "Baybrook Mall",
          "University of Houston - Clear Lake", "Clear Lake High School"]


def geocode(c, q):
    r = c.get(f"{API}/geocode", params={"q": q, "limit": 1}).json()["results"]
    return (r[0]["label"], r[0]["lat"], r[0]["lon"]) if r else None


def route(c, a, b):
    r = c.get(f"{API}/route", params={"from": f"{a[0]},{a[1]}", "to": f"{b[0]},{b[1]}"})
    return r.json() if r.status_code == 200 else None


def line_m(geom):
    return transform(to_m, LineString(geom["coordinates"]))


def replay_ignore(c, d, dest):
    """Car follows the original grey route; count re-plans like the page does."""
    grey = line_m(d["baseline"]["geometry"])
    blue = line_m(d["flood_aware"]["geometry"])
    step, since, replans = 25 / FT, 0.0, 0
    to_ll = Transformer.from_crs(2278, 4326, always_xy=True).transform
    s = 0.0
    while s < grey.length:
        p = grey.interpolate(s)
        since += step
        if blue.distance(p) * FT > 50 and since * FT >= 320:
            lon, lat = to_ll(p.x, p.y)
            nd = route(c, (lat, lon), dest)
            if not nd:
                break
            blue = line_m(nd["flood_aware"]["geometry"])
            replans += 1
            since = 0.0
            if replans >= 15:
                break
        s += step
    return replans


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=6)
    args = ap.parse_args()
    with httpx.Client(timeout=60) as c:
        pts = [g for g in (geocode(c, q) for q in PLACES) if g]
        rows = []
        for a, b in itertools.combinations(pts, 2):
            km = math.hypot((a[2] - b[2]) * 96.5, (a[1] - b[1]) * 111.3)
            if not 4 <= km <= 15:
                continue
            for s, e in ((a, b), (b, a)):
                d = route(c, s[1:], e[1:])
                if not d:
                    continue
                grey, blue = line_m(d["baseline"]["geometry"]), line_m(d["flood_aware"]["geometry"])
                hs = d["hotspots_avoided"]
                rows.append({
                    "from": s[0], "to": e[0], "a": s[1:], "b": e[1:],
                    "base_km": d["baseline"]["distance_m"] / 1000, "added_s": d["detour"]["added_s"],
                    "avoided": len(hs), "high_avoided": sum(1 for h in hs if h["tier"] == "high"),
                    "medium_avoided": sum(1 for h in hs if h["tier"] == "medium"),
                    "spread_m": round(grey.hausdorff_distance(blue) * FT),
                    "high_exposure_cut": d["risk_exposure_m"]["baseline"]["high"] - d["risk_exposure_m"]["flood_aware"]["high"],
                    "d": d,
                })
            print(f"\r{len(rows)} trips evaluated", end="", flush=True, file=sys.stderr)
        print(file=sys.stderr)
        # follow-advice: visible detour around high-risk areas at small cost
        follow = sorted([r for r in rows if r["high_avoided"] >= 1 and r["added_s"] <= 150 and r["spread_m"] >= 250
                         and 5 <= r["base_km"] <= 13],
                        key=lambda r: (-(r["high_avoided"] * 3 + r["medium_avoided"]), r["added_s"]))[:args.top * 2]
        # ignore-advice: replay the top candidates by divergence and count re-plans
        pool = sorted([r for r in rows if r["avoided"] >= 4 and 5 <= r["base_km"] <= 13],
                      key=lambda r: -(r["avoided"] + r["spread_m"] / 500))[:40]
        for r in pool:
            r["replans"] = replay_ignore(c, r["d"], r["b"])
        ignore = sorted([r for r in pool if 3 <= r["replans"] <= 8],
                        key=lambda r: (-r["replans"], -r["avoided"]))[:args.top]
        clear_lake = [r for r in rows if "Clear Lake" in r["from"] + r["to"] or "Baybrook" in r["from"] + r["to"]]

    def link(r, extra=""):
        from urllib.parse import quote
        return (f"https://floodroutes.nbolt.com/?from={r['a'][0]:.6f},{r['a'][1]:.6f}&fromLabel={quote(r['from'])}"
                f"&to={r['b'][0]:.6f},{r['b'][1]:.6f}&toLabel={quote(r['to'])}{extra}")

    L = ["# Demo trip candidates\n", "Generated by `validate/find_demos.py` against the local API.\n"]
    L += ["## Ignore-advice demos (several re-plans)\n",
          "| From | To | km | Re-plans | Hotspots avoided (high/med) | Added | Link |", "|---|---|---:|---:|---|---:|---|"]
    for r in ignore:
        L.append(f"| {r['from']} | {r['to']} | {r['base_km']:.1f} | {r['replans']} | {r['avoided']} ({r['high_avoided']}/{r['medium_avoided']}) "
                 f"| {r['added_s']:+d} s | [sim](<{link(r, '&sim=ignore')}>) |")
    L += ["", "## Follow-advice demos (visible detour around high-risk areas)\n",
          "| From | To | km | Hotspots avoided (high/med) | High-risk metres cut | Route spread | Added | Link |",
          "|---|---|---:|---|---:|---:|---:|---|"]
    for r in follow[:args.top]:
        L.append(f"| {r['from']} | {r['to']} | {r['base_km']:.1f} | {r['avoided']} ({r['high_avoided']}/{r['medium_avoided']}) "
                 f"| {r['high_exposure_cut']} m | {r['spread_m']} m | {r['added_s']:+d} s | [sim](<{link(r, '&sim=1')}>) |")
    L += ["", "## Clear Lake trips (firefighter-report area around Station 72)\n",
          "| From | To | km | Hotspots avoided | Added | Link |", "|---|---|---:|---:|---:|---|"]
    for r in sorted(clear_lake, key=lambda r: -r["avoided"])[:args.top]:
        L.append(f"| {r['from']} | {r['to']} | {r['base_km']:.1f} | {r['avoided']} | {r['added_s']:+d} s | [map](<{link(r)}>) |")
    DOC.write_text("\n".join(L) + "\n")
    print(f"wrote {DOC}: {len(rows)} trips, {len(pool)} replayed")


if __name__ == "__main__":
    main()
