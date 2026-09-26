#!/usr/bin/env python3
"""Compare HFD stations that officers say get cut off in rain (Kinder/Rice 2019
survey, Appendix A) with the 311-based risk surface around every station.

Station locations: OSM amenity=fire_station named "Houston Fire Department Station N".
Writes docs/hfd_stations.md.
"""
import json
import re
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
from db import connect  # noqa: E402

PBF = ROOT / "data" / "osm" / "houston.osm.pbf"
DOC = ROOT / "docs" / "hfd_stations.md"

# From Appendix A of "Measuring Flooding with the Houston Fire Department" (Stein & Perry, Kinder/Rice, 2019).
# "cut off" = officers say units cannot leave / station isolated / water at the station in ordinary heavy rain;
# "harvey" = cut off or flooded in Harvey only.
REPORTED = {
    16: ("cut off", "cannot leave to make runs: flooding right outside the station"),
    31: ("cut off", "Crosstimbers floods in front of the station; units locked in"),
    52: ("cut off", "usually cannot leave the station because Hartsook floods"),
    68: ("cut off", "flooded into the station by the adjacent bayou, last three years"),
    45: ("cut off", "station becomes an island; cannot get off McCarty Rd"),
    28: ("cut off", "ambulance and chief's car cannot leave after heavy rain (driveway slope)"),
    26: ("cut off", "station flooded 3-5 times; Dixie Dr under several feet"),
    7: ("cut off", "Elgin at Austin floods in front of the station, deeper over 8 years"),
    35: ("cut off", "flooding in front of the station delays response"),
    72: ("cut off", "Bay Area Blvd at Saturn Ln floods to the station front"),
    5: ("cut off", "water twice in 10 years covered the lot; vehicles moved inside"),
    83: ("cut off", "Richmond west of the Beltway floods, incl. Breezewood where the station is"),
    39: ("harvey", "Harvey: all area around the station flooded, no access"),
    65: ("harvey", "Harvey: station isolated by street flooding"),
    69: ("harvey", "Harvey: could not cross Buffalo Bayou"),
    50: ("dry", "Harvey: area around the station high and dry for 1,000 ft"),
    3: ("dry", "no street-flooding issues around the station"),
}


def stations():
    with tempfile.TemporaryDirectory() as d:
        pbf = Path(d) / "f.osm.pbf"
        subprocess.run(["osmium", "tags-filter", "-O", "-o", str(pbf), str(PBF), "nwr/amenity=fire_station"], check=True)
        out = subprocess.run(["osmium", "export", "-f", "geojsonseq", "--geometry-types=point,polygon", str(pbf)],
                             check=True, capture_output=True, text=True).stdout
    from shapely.geometry import shape
    st = {}
    for ln in out.splitlines():
        ln = ln.strip("\x1e").strip()
        if not ln:
            continue
        f = json.loads(ln)
        m = re.fullmatch(r"Houston Fire Department Station (\d{1,3})", f["properties"].get("name", "") or "")
        if m:
            c = shape(f["geometry"]).centroid
            st.setdefault(int(m.group(1)), (c.y, c.x))
    return st


def main():
    st = stations()
    rows = []
    with connect() as conn:
        for num, (lat, lon) in sorted(st.items()):
            r = conn.execute("""
                WITH p AS (SELECT ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography g)
                SELECT
                  coalesce(sum(coalesce(h.score_311, h.score)) FILTER (WHERE ST_DWithin(h.geom::geography, p.g, 300)), 0),
                  coalesce(sum(coalesce(h.score_311, h.score)) FILTER (WHERE ST_DWithin(h.geom::geography, p.g, 1000)), 0),
                  coalesce(max(coalesce(h.score_311, h.score)) FILTER (WHERE ST_DWithin(h.geom::geography, p.g, 300)), 0),
                  (SELECT min(ST_Distance(rp.geom::geography, p.g)) FROM core.risk_polygon rp
                    WHERE ST_DWithin(rp.geom::geography, p.g, 3000)),
                  (SELECT rp.tier FROM core.risk_polygon rp WHERE ST_DWithin(rp.geom::geography, p.g, 3000)
                    ORDER BY ST_Distance(rp.geom::geography, p.g) LIMIT 1),
                  (SELECT count(*) FROM core.risk_polygon rp WHERE rp.tier IN ('high', 'medium')
                    AND ST_DWithin(rp.geom::geography, p.g, 1000))
                FROM p LEFT JOIN core.risk_hex h ON ST_DWithin(h.geom::geography, p.g, 1000)
                GROUP BY p.g""", (lon, lat)).fetchone()
            rows.append(dict(num=num, lat=lat, lon=lon, s300=r[0], s1k=r[1], max300=r[2],
                             dist=r[3], tier=r[4], hm1k=r[5]))

    def pctile(v, xs):
        return 100 * sum(1 for x in xs if x <= v) / len(xs)

    all1k = [r["s1k"] for r in rows]
    all300 = [r["s300"] for r in rows]
    L = []
    w = L.append
    w("# HFD stations vs the 311 risk surface\n")
    w("**Source of the comments:** *Measuring Flooding with the Houston Fire Department*, S. Perry & R. Stein, "
      "Rice University / Kinder Institute, report to HFD (2019). Survey of 201 officers at all 88 stations, Sept–Oct 2018. "
      "Appendix A holds officer comments; the 1,383 surveyed flood locations are not published. "
      "Page: https://kinder.rice.edu/research/measuring-flooding-houston-fire-department\n")
    w(f"**Station locations:** OSM (`amenity=fire_station`, named \"Houston Fire Department Station N\"): "
      f"{len(rows)} stations found. **Risk:** sum of 311 risk-hex scores within 300 m and 1 km of the station, "
      "and its percentile among all stations (100 = most 311 flood history around it). Scores use the "
      "311-only hex score (`score_311`), not the firefighter minimums added from these same comments, so the "
      "comparison stays independent. \"Nearest risk area\" uses the final tiers, which do include them.\n")
    w("| Station | Officers' report | Score ≤300 m | pctile | Score ≤1 km | pctile | Nearest risk area | High/medium areas ≤1 km |")
    w("|---:|---|---:|---:|---:|---:|---|---:|")
    by = {r["num"]: r for r in rows}
    cut = []
    for num, (kind, text) in sorted(REPORTED.items(), key=lambda kv: ({"cut off": 0, "harvey": 1, "dry": 2}[kv[1][0]], kv[0])):
        r = by.get(num)
        if not r:
            w(f"| {num} | *{kind}*: {text} | – | – | – | – | not in OSM | – |")
            continue
        if kind == "cut off":
            cut.append(r)
        near = f"{r['tier']} at {r['dist']:.0f} m" if r["dist"] is not None else "none within 3 km"
        w(f"| {num} | *{kind}*: {text} | {r['s300']:.0f} | {pctile(r['s300'], all300):.0f} | {r['s1k']:.0f} | "
          f"{pctile(r['s1k'], all1k):.0f} | {near} | {r['hm1k']} |")
    w("")
    if cut:
        cut_p = [pctile(r["s1k"], all1k) for r in cut]
        low = [r["num"] for r in cut if pctile(r["s1k"], all1k) < 50]
        w("## Summary\n")
        w(f"- The {len(cut)} stations officers describe as cut off in ordinary heavy rain have a median 1 km risk "
          f"percentile of **{statistics.median(cut_p):.0f}** (50 = typical station).")
        w(f"- {sum(1 for p in cut_p if p >= 75)} of {len(cut)} are in the top quarter of stations by nearby 311 flood history; "
          f"{len(low)} are below the median" + (f" (stations {', '.join(map(str, low))})." if low else "."))
        w("- Below-median stations are where 311 under-sees flooding that firefighters experience: a station's own "
          "access road (Hartsook at 52, McCarty at 45, Bay Area Blvd at 72, Dixie Dr at 26, Richmond at 83) can flood "
          "without many residents calling it in. Station 72 is in Clear Lake, where the model shows almost nothing.")
        w("- Caveats: the survey is from 2018 and station locations are today's OSM points (a few stations may have "
          "moved; station 65 is not in OSM). Station 83 has two conflicting comments. Twelve stations is a small sample.")
        w("- Station-access flooding is a routing problem in reverse: the question is not which street to avoid "
          "on the way somewhere, but whether a unit can leave at all. That needs live data (gauges, sensors, "
          "or HFD's own reports), not history alone.\n")
    w("## All stations (ranked by risk within 1 km)\n")
    w("| Station | Score ≤300 m | Score ≤1 km | Nearest risk area |")
    w("|---:|---:|---:|---|")
    for r in sorted(rows, key=lambda r: -r["s1k"]):
        near = f"{r['tier']} at {r['dist']:.0f} m" if r["dist"] is not None else "none within 3 km"
        w(f"| {r['num']} | {r['s300']:.0f} | {r['s1k']:.0f} | {near} |")
    DOC.write_text("\n".join(L) + "\n")
    print(f"wrote {DOC}: {len(rows)} stations")


if __name__ == "__main__":
    main()
