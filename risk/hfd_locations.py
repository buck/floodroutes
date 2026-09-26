#!/usr/bin/env python3
"""Map the firefighter comments in risk/hfd_comments.yaml onto OSM roads.

Writes risk/hfd_locations.geojson: one feature per mapped comment (LineString /
MultiLineString / Point) with station, severity and quote, plus a report of
anything that could not be mapped. Street matches nearest the commenting
station win; segments follow GraphHopper's car route between the two junctions
and are accepted only if they stay on the named street.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import httpx
import yaml
from pyproj import Transformer
from shapely.geometry import LineString, MultiLineString, Point, mapping, shape
from shapely.ops import transform, unary_union

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "validate"))
from hfd_stations import stations  # noqa: E402

PBF = ROOT / "data" / "osm" / "houston.osm.pbf"
ROADS = ROOT / "data" / "osm" / "named_roads.geojson"
SRC = ROOT / "risk" / "hfd_comments.yaml"
OUT = ROOT / "risk" / "hfd_locations.geojson"
GH = "http://127.0.0.1:8989"
SEARCH_M = 6000          # look for streets within this distance of the station
to_ft = Transformer.from_crs(4326, 2278, always_xy=True).transform
to_ll = Transformer.from_crs(2278, 4326, always_xy=True).transform
FT = 0.3048006096


def named_roads():
    if not ROADS.exists() or ROADS.stat().st_mtime < PBF.stat().st_mtime:
        tmp = ROADS.with_suffix(".osm.pbf")
        subprocess.run(["osmium", "tags-filter", "-O", "-o", str(tmp), str(PBF), "w/highway"], check=True)
        out = subprocess.run(["osmium", "export", "-f", "geojsonseq", "--geometry-types=linestring", str(tmp)],
                             check=True, capture_output=True, text=True).stdout
        tmp.unlink()
        feats = []
        for ln in out.splitlines():
            ln = ln.strip("\x1e").strip()
            if not ln:
                continue
            f = json.loads(ln)
            p = f["properties"]
            if p.get("name") and p.get("highway") and p["highway"] not in ("footway", "path", "cycleway", "steps", "pedestrian"):
                feats.append({"type": "Feature", "properties": {"name": p["name"], "highway": p["highway"]},
                              "geometry": f["geometry"]})
        ROADS.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    roads = []
    for f in json.loads(ROADS.read_text())["features"]:
        roads.append((f["properties"]["name"], transform(to_ft, shape(f["geometry"]))))
    return roads


def street(roads, pattern, near, radius_m=SEARCH_M):
    rx = re.compile(pattern, re.I)
    r_ft = radius_m / FT
    parts = [g for n, g in roads if rx.search(n) and (near is None or g.distance(near) <= r_ft)]
    return unary_union(parts) if parts else None


def points_of(geom):
    if geom.is_empty:
        return []
    if geom.geom_type == "Point":
        return [geom]
    if hasattr(geom, "geoms"):
        return [g if g.geom_type == "Point" else g.centroid for g in geom.geoms]
    return [geom.centroid]


def junction(roads, a, b, near):
    ga, gb = street(roads, a, near), street(roads, b, near)
    if ga is None or gb is None:
        return None, f"street not found: {a if ga is None else b}"
    pts = points_of(ga.intersection(gb))
    if not pts:
        # divided roads and slight OSM gaps: accept a near-miss up to 30 m
        if ga.distance(gb) < 30 / FT:
            from shapely.ops import nearest_points
            p1, p2 = nearest_points(ga, gb)
            pts = [Point((p1.x + p2.x) / 2, (p1.y + p2.y) / 2)]
        else:
            return None, f"{a} and {b} do not meet"
    ref = near or unary_union(pts).centroid
    pts.sort(key=lambda p: p.distance(ref))
    # cluster the junction points of divided roads (within 60 m of the best one)
    best = [p for p in pts if p.distance(pts[0]) < 60 / FT]
    return unary_union(best).centroid, None


def route_along(client, p1, p2, street_geom):
    a, b = transform(to_ll, p1), transform(to_ll, p2)
    r = client.get(f"{GH}/route", params={"point": [f"{a.y},{a.x}", f"{b.y},{b.x}"], "profile": "car",
                                          "points_encoded": "false", "instructions": "false"})
    if r.status_code != 200:
        return None, "GraphHopper could not route between the junctions"
    line = transform(to_ft, LineString(r.json()["paths"][0]["points"]["coordinates"]))
    on = line.intersection(street_geom.buffer(40 / FT)).length / max(line.length, 1e-9)
    if on < 0.8:
        return None, f"route leaves the street ({on:.0%} on it)"
    return line, None


def main():
    items = yaml.safe_load(SRC.read_text())["items"]
    st = {n: transform(to_ft, Point(lon, lat)) for n, (lat, lon) in stations().items()}
    roads = named_roads()
    feats, problems = [], []
    with httpx.Client(timeout=30) as client:
        for it in items:
            near = st.get(it["station"]) if it.get("station") else None
            if it.get("station") and near is None:
                problems.append((it["id"], f"station {it['station']} not in OSM"))
                continue
            geom, err = None, None
            if it["kind"] == "intersection":
                geom, err = junction(roads, it["streets"][0], it["streets"][1], near)
            elif it["kind"] == "station_front":
                g = street(roads, it["street"], near, it["radius_m"])
                geom = g.intersection(near.buffer(it["radius_m"] / FT)) if g is not None else None
                if geom is None or geom.is_empty:
                    geom, err = None, f"no '{it['street']}' within {it['radius_m']} m of the station"
            elif it["kind"] == "segment":
                p1, e1 = junction(roads, it["street"], it["from"], near)
                p2, e2 = junction(roads, it["street"], it["to"], near or p1)
                if e1 or e2:
                    err = e1 or e2
                else:
                    geom, err = route_along(client, p1, p2, street(roads, it["street"], p1))
            if err:
                problems.append((it["id"], err))
                continue
            geom_ll = transform(to_ll, geom)
            length_m = geom.length * FT if geom.geom_type != "Point" else 0
            feats.append({"type": "Feature", "id": it["id"],
                          "properties": {"id": it["id"], "station": it.get("station"), "kind": it["kind"],
                                         "severity": it["severity"], "quote": it["quote"],
                                         "length_m": round(length_m)},
                          "geometry": mapping(geom_ll)})
            print(f"ok   {it['id']:28s} {it['kind']:13s} {geom.geom_type:15s} {length_m:6.0f} m")
    OUT.write_text(json.dumps({"type": "FeatureCollection",
                               "source": "Perry & Stein, Measuring Flooding with the Houston Fire Department "
                                         "(Rice/Kinder, 2019), Appendix A",
                               "features": feats}, separators=(",", ":")))
    for pid, err in problems:
        print(f"FAIL {pid:28s} {err}")
    print(f"wrote {OUT}: {len(feats)} mapped, {len(problems)} not mapped")


if __name__ == "__main__":
    main()
