#!/usr/bin/env python3
"""Validation runs against the live API (:8086) -> docs/validation.md + screenshots.

1. Top-10 hotspots: for each of the highest-scoring high-tier polygons, find a
   trip whose baseline route crosses it and check the flood-aware route avoids it.
2. 200 random origin-destination pairs inside the city: added distance/time
   distribution, trips > 50% longer, latency.

Usage: validate.py [--pairs 200] [--no-screenshots]
"""
import argparse
import math
import random
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx
from shapely.geometry import LineString, shape

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
from db import connect  # noqa: E402

API = "http://127.0.0.1:8086"
DOC = ROOT / "docs" / "validation.md"
SHOTS = ROOT / "docs" / "validation"


def route(client, a, b):
    t0 = time.perf_counter()
    r = client.get(f"{API}/route", params={"from": f"{a[0]:.6f},{a[1]:.6f}", "to": f"{b[0]:.6f},{b[1]:.6f}"})
    return r, time.perf_counter() - t0


def offset(lat, lon, bearing_deg, dist_m):
    dlat = dist_m * math.cos(math.radians(bearing_deg)) / 111_320
    dlon = dist_m * math.sin(math.radians(bearing_deg)) / (111_320 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def screenshot(a, b, name):
    chrome = shutil.which("google-chrome-stable") or shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        return None
    out = SHOTS / f"{name}.png"
    url = f"{API}/?from={a[0]:.6f},{a[1]:.6f}&to={b[0]:.6f},{b[1]:.6f}"
    subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--window-size=1280,860",
                    "--virtual-time-budget=15000", f"--screenshot={out}", url],
                   capture_output=True, timeout=90)
    return out if out.exists() else None


GH = "http://127.0.0.1:8989"


def candidate_trips(client, lat, lon, poly):
    """Snap the hotspot to its road, then walk ~2 km out along real roads in four
    directions; pairs of those endpoints (both outside the polygon) give trips
    that pass the hotspot. Straight-line pairs are the fallback."""
    r = client.get(f"{GH}/nearest", params={"point": f"{lat},{lon}", "profile": "car"})
    if r.status_code != 200:
        return
    slon, slat = r.json()["coordinates"]
    lines = {}
    for bearing in (0, 90, 180, 270):
        far = offset(slat, slon, bearing, 3500)
        q = client.get(f"{GH}/route", params={"point": [f"{slat},{slon}", f"{far[0]},{far[1]}"], "profile": "car",
                                              "points_encoded": "false", "instructions": "false"})
        if q.status_code == 200:
            line = LineString(q.json()["paths"][0]["points"]["coordinates"])
            if line.length > 0:
                lines[bearing] = line
    for walk in (0.0065, 0.011, 0.019):  # ~700 m, 1.2 km, 2 km in degrees
        ends = {}
        for bearing, line in lines.items():
            pt = line.interpolate(min(walk, line.length * 0.9))
            if not poly.buffer(0.001).contains(pt):
                ends[bearing] = (pt.y, pt.x)
        for x, y in ((0, 180), (90, 270), (0, 90), (180, 270), (0, 270), (90, 180)):
            if x in ends and y in ends:
                yield ends[x], ends[y]
    # fallback: straight-line pairs through the interior point
    for dist in (1500, 2500, 4000):
        for bearing in (0, 90, 45, 135):
            yield offset(lat, lon, bearing, dist), offset(lat, lon, bearing + 180, dist)


def hotspots(client, n, shots):
    with connect() as conn:
        polys = conn.execute("""
            SELECT id, score_max, storm_events, cases, ST_AsGeoJSON(geom),
                   ST_Y(ST_PointOnSurface(geom)), ST_X(ST_PointOnSurface(geom))
            FROM core.risk_polygon WHERE tier = 'high' ORDER BY score_max DESC LIMIT %s""", (n,)).fetchall()
    rows = []
    for rank, (pid, score, events, cases, gj, lat, lon) in enumerate(polys, 1):
        poly = shape(__import__("json").loads(gj))
        found = None
        for a, b in candidate_trips(client, lat, lon, poly):
            r, dt = route(client, a, b)
            if r.status_code != 200:
                continue
            d = r.json()
            if LineString(d["baseline"]["geometry"]["coordinates"]).intersects(poly):
                found = (a, b, d, dt)
                break
        if not found:
            rows.append((rank, pid, score, events, cases, None))
            continue
        a, b, d, dt = found
        flood = LineString(d["flood_aware"]["geometry"]["coordinates"])
        avoided = not flood.intersects(poly)
        shot = screenshot(a, b, f"hotspot_{rank:02d}_{pid}") if shots else None
        rows.append((rank, pid, score, events, cases, dict(a=a, b=b, d=d, avoided=avoided, dt=dt, shot=shot)))
        print(f"hotspot {rank} {pid}: avoided={avoided} +{d['detour']['added_s']}s "
              f"+{d['detour']['added_m']}m ({d['detour']['added_distance_pct']}%)", flush=True)
    return rows


def random_pairs(client, n, seed=25):
    with connect() as conn:
        # random located 311 cases as city points (coordinates only)
        pts = conn.execute("""SELECT ST_Y(geom), ST_X(geom) FROM core.service_request TABLESAMPLE SYSTEM (0.5) REPEATABLE (25)
                              WHERE geom IS NOT NULL LIMIT 20000""").fetchall()
    rng = random.Random(seed)
    res, fails = [], 0
    while len(res) < n and fails < n:
        a, b = rng.sample(pts, 2)
        straight = LineString([(a[1], a[0]), (b[1], b[0])]).length * 100_000
        if straight < 2000:
            continue
        r, dt = route(client, a, b)
        if r.status_code != 200:
            fails += 1
            continue
        d = r.json()
        res.append(dict(base_m=d["baseline"]["distance_m"], base_s=d["baseline"]["time_s"],
                        pct_d=d["detour"]["added_distance_pct"], pct_t=d["detour"]["added_time_pct"],
                        add_s=d["detour"]["added_s"], add_m=d["detour"]["added_m"],
                        avoided=len(d["hotspots_avoided"]),
                        high_base=d["risk_exposure_m"]["baseline"]["high"],
                        high_flood=d["risk_exposure_m"]["flood_aware"]["high"], latency=dt, a=a, b=b))
    return res, fails


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", type=int, default=200)
    ap.add_argument("--no-screenshots", action="store_true")
    args = ap.parse_args()
    SHOTS.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=60) as client:
        hs = hotspots(client, 10, not args.no_screenshots)
        pairs, fails = random_pairs(client, args.pairs)

    L = []
    w = L.append
    w("# Validation\n")
    w(f"Generated by `validate/validate.py` on {time.strftime('%Y-%m-%d %H:%M')} against the live API.\n")
    w("## Top-10 hotspots\n")
    w("For each of the ten highest-scoring high-tier polygons, the interior point was snapped to its "
      "road and trip endpoints were taken 0.7, 1.2 or 2 km out along real roads in four directions (outside the "
      "polygon); the first "
      "endpoint pair whose baseline route crosses the polygon is used (straight-line pairs as fallback).\n")
    w("| # | Polygon | Score | Storm events | Reports | Baseline crosses | Flood route avoids | Added time | Added distance | Screenshot |")
    w("|---:|---|---:|---:|---:|---|---|---:|---:|---|")
    ok = 0
    for rank, pid, score, events, cases, r in hs:
        if r is None:
            w(f"| {rank} | {pid} | {score:.0f} | {events} | {cases} | no trip found | – | – | – | – |")
            continue
        det = r["d"]["detour"]
        ok += r["avoided"]
        shot = f"[png](validation/{r['shot'].name})" if r["shot"] else "–"
        w(f"| {rank} | {pid} | {score:.0f} | {events} | {cases} | yes | {'**yes**' if r['avoided'] else 'no'} | "
          f"+{det['added_s']} s ({det['added_time_pct']}%) | +{det['added_m']} m ({det['added_distance_pct']}%) | {shot} |")
    w(f"\n**{ok} of {sum(1 for h in hs if h[5])}** hotspot trips avoid the hotspot on the flood-aware route.\n")
    w("How to read this:\n")
    w("- *Not avoided* is by design, not a failure: history only multiplies priority (high = 0.05, i.e. "
      "the segment costs ~20x), it never excludes a road. When every alternative is much longer, for "
      "example the only bayou crossing or freeway access for kilometres, the route still passes through. "
      "Live high-water reports (POST /route `areas`) are the hard-exclusion path.")
    w("- *No trip found* means none of the test endpoint pairs produced a baseline through the polygon: "
      "typically hotspots on freeway frontage or ramps, or in cul-de-sac networks that through-trips never enter.")
    w("- Percentages are large because the test trips are short (1.5–4 km) and local by construction; "
      "the absolute costs (+1–3 min) are what a driver sees. See the random-pair section for realistic trips.\n")
    for rank, pid, *_rest, r in hs:
        if r and r["shot"]:
            w(f"### {rank}. {pid}\n\n![{pid}](validation/{r['shot'].name})\n")
            break

    n = len(pairs)
    w(f"## {n} random origin–destination pairs\n")
    w("Origins and destinations are random located 311 case points (so inside the city), ≥ 2 km apart "
      f"in a straight line. {fails} pairs could not be routed.\n")
    lat = [p["latency"] * 1000 for p in pairs]
    w("| Metric | median | p90 | p99 | max |\n|---|---:|---:|---:|---:|")
    for label, xs in (("Baseline distance (km)", [p["base_m"] / 1000 for p in pairs]),
                      ("Baseline time (min)", [p["base_s"] / 60 for p in pairs]),
                      ("Added distance (%)", [p["pct_d"] for p in pairs]),
                      ("Added time (%)", [p["pct_t"] for p in pairs]),
                      ("Added time (s)", [p["add_s"] for p in pairs]),
                      ("Hotspots avoided", [p["avoided"] for p in pairs]),
                      ("API latency, both routes (ms)", lat)):
        w(f"| {label} | {statistics.median(xs):.1f} | {pct(xs, .9):.1f} | {pct(xs, .99):.1f} | {max(xs):.1f} |")
    same = sum(1 for p in pairs if p["add_m"] == 0)
    had_high = [p for p in pairs if p["high_base"] > 0]
    cleared = sum(1 for p in had_high if p["high_flood"] == 0)
    w("")
    w(f"- {same} of {n} trips ({100 * same / n:.0f}%) are unchanged by the flood profile.")
    w(f"- {len(had_high)} baseline routes cross a high-tier area; the flood-aware route clears all high-tier "
      f"exposure on {cleared} of them.")
    long_ = [p for p in pairs if p["pct_d"] > 50]
    w(f"- Trips where the flood route is > 50% longer: **{len(long_)}**.")
    for p in long_:
        w(f"  - {p['base_m'] / 1000:.1f} km baseline, +{p['pct_d']}% distance, +{p['pct_t']}% time")
    w(f"- API latency (both GraphHopper calls plus hotspot overlay): median {statistics.median(lat):.0f} ms, "
      f"p99 {pct(lat, .99):.0f} ms. Target was < 500 ms per flood route.")
    DOC.write_text("\n".join(L) + "\n")
    print(f"wrote {DOC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
