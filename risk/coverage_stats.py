#!/usr/bin/env python3
"""How much of Houston is in each risk tier: share of city area and of road length.

City limits and roads come from the OSM extract (osmium must be on PATH).
Prints markdown tables for the README.
"""
import collections
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from pyproj import Transformer
from shapely import prepared
from shapely.geometry import shape
from shapely.ops import transform, unary_union

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
from db import connect  # noqa: E402

PBF = ROOT / "data" / "osm" / "houston.osm.pbf"
FT = 0.3048006096
TIERS = ("high", "medium", "low")
ROADS = ("motorway,trunk,primary,secondary,tertiary,unclassified,residential,motorway_link,trunk_link,"
         "primary_link,secondary_link,tertiary_link,living_street,service")
to_ft = Transformer.from_crs(4326, 2278, always_xy=True).transform


def osm_features(filter_expr):
    with tempfile.TemporaryDirectory() as d:
        pbf = Path(d) / "f.osm.pbf"
        subprocess.run(["osmium", "tags-filter", "-O", "-o", str(pbf), str(PBF), filter_expr], check=True)
        out = subprocess.run(["osmium", "export", "-f", "geojsonseq", str(pbf)], check=True,
                             capture_output=True, text=True).stdout
    for ln in out.splitlines():
        ln = ln.strip("\x1e").strip()
        if ln:
            yield json.loads(ln)


def road_class(hw):
    if hw == "service":
        return "Service roads, driveways and lots"
    if hw.startswith(("motorway", "trunk")):
        return "Freeways"
    if hw.startswith(("primary", "secondary", "tertiary")):
        return "Major roads"
    return "Local streets"


def main():
    city = next(transform(to_ft, shape(f["geometry"])) for f in osm_features("r/boundary=administrative")
                if f["properties"].get("name") == "Houston" and f["properties"].get("admin_level") == "8")
    city_km2 = city.area * FT ** 2 / 1e6
    with connect() as c:
        polys = c.execute("SELECT tier, ST_AsGeoJSON(geom) FROM core.risk_polygon").fetchall()
        any_hex_km2 = c.execute("SELECT sum(ST_Area(geom::geography)) / 1e6 FROM core.risk_hex").fetchone()[0]
        n_hex = c.execute("SELECT count(*) FROM core.risk_hex").fetchone()[0]
    tiers = {t: unary_union([transform(to_ft, shape(json.loads(g))) for tt, g in polys if tt == t]) for t in TIERS}

    area = {t: tiers[t].intersection(city).area * FT ** 2 / 1e6 for t in TIERS}
    cp = prepared.prep(city)
    length = collections.Counter()
    inside = collections.Counter()
    for f in osm_features(f"w/highway={ROADS}"):
        if f["geometry"]["type"] != "LineString":
            continue
        g = transform(to_ft, shape(f["geometry"]))
        if not cp.intersects(g):
            continue
        g = g.intersection(city)
        cls = road_class(f["properties"]["highway"])
        length[cls] += g.length
        for t in TIERS:
            inside[(cls, t)] += g.intersection(tiers[t]).length

    total_len = sum(length.values())
    print(f"City of Houston (OSM admin boundary): {city_km2:,.0f} km²; "
          f"{total_len * FT / 1000:,.0f} km of drivable road.\n")
    print("| Tier | Area | Share of city area | Share of road length |")
    print("|---|---:|---:|---:|")
    for t in TIERS:
        rl = sum(inside[(c, t)] for c in length)
        print(f"| {t.capitalize()} | {area[t]:.1f} km² | {100 * area[t] / city_km2:.2f}% | {100 * rl / total_len:.2f}% |")
    a_all = sum(area.values())
    r_all = sum(inside.values())
    print(f"| **Any tier** | {a_all:.1f} km² | **{100 * a_all / city_km2:.2f}%** | **{100 * r_all / total_len:.2f}%** |\n")
    print("| Road type | km | High | Medium | Low | Any tier |")
    print("|---|---:|---:|---:|---:|---:|")
    for cls in ("Freeways", "Major roads", "Local streets", "Service roads, driveways and lots"):
        L = length[cls]
        v = [100 * inside[(cls, t)] / L for t in TIERS]
        print(f"| {cls} | {L * FT / 1000:,.0f} | {v[0]:.2f}% | {v[1]:.2f}% | {v[2]:.2f}% | {sum(v):.1f}% |")
    print(f"\nHexes with at least one flood signal: {n_hex:,} ({any_hex_km2:,.0f} km², "
          f"~{100 * any_hex_km2 / city_km2:.0f}% of city area; not clipped to city limits).")


if __name__ == "__main__":
    main()
