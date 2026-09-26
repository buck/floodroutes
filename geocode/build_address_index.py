#!/usr/bin/env python3
"""Build the self-hosted address search index: data/geocoder.sqlite.

Sources (all public, all local; nothing is looked up online at query time):
  - US Census TIGER/Line 2025 ADDRFEAT address ranges for the eight counties the
    routing graph covers (data/external/tiger/*.zip): every street segment with its
    house-number range and ZIP on each side. Addresses are interpolated along the block.
  - OpenStreetMap exact address points from the Houston extract, preferred where present.
  - OpenStreetMap named places (neighbourhoods, universities, hospitals, parks, airports, ...).

Used by api/app.py (/geocode, /reverse) through the stdlib sqlite3 module.
"""
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TIGER = sorted((ROOT / "data" / "external" / "tiger").glob("tl_*_addrfeat.zip"))
PBF = ROOT / "data" / "osm" / "houston.osm.pbf"
OUT = ROOT / "data" / "geocoder.sqlite"
BBOX = (-96.2, 29.2, -94.7, 30.4)

sys.path.insert(0, str(ROOT / "api"))
from addrnorm import norm_street  # noqa: E402  (shared with the API)

POI_TAGS = {
    "place": {"neighbourhood", "suburb", "quarter", "city", "town", "village"},
    "amenity": {"university", "college", "school", "hospital", "library", "fire_station", "police",
                "townhall", "community_centre", "clinic", "courthouse"},
    "tourism": {"museum", "zoo", "attraction", "theme_park", "stadium"},
    "leisure": {"park", "stadium", "sports_centre"},
    "aeroway": {"aerodrome"},
    "shop": {"mall"},
    "railway": {"station"},
}


def tiger_features():
    for z in TIGER:
        out = subprocess.run(["ogr2ogr", "-f", "GeoJSONSeq", "-t_srs", "EPSG:4326", "/vsistdout/", f"/vsizip/{z}"],
                             check=True, capture_output=True, text=True).stdout
        for ln in out.splitlines():
            ln = ln.strip("\x1e").strip()
            if ln:
                yield json.loads(ln)


def osm_features(filters):
    tmp = OUT.with_suffix(".tmp.osm.pbf")
    subprocess.run(["osmium", "tags-filter", "-O", "-o", str(tmp), str(PBF), *filters], check=True)
    out = subprocess.run(["osmium", "export", "-f", "geojsonseq", "--geometry-types=point,polygon", str(tmp)],
                         check=True, capture_output=True, text=True).stdout
    tmp.unlink()
    for ln in out.splitlines():
        ln = ln.strip("\x1e").strip()
        if ln:
            yield json.loads(ln)


def num(s):
    m = re.match(r"^\s*(\d+)", s or "")
    return int(m.group(1)) if m else None


def point_of(geom):
    c = geom["coordinates"]
    if geom["type"] == "Point":
        return c
    while isinstance(c[0][0], list):
        c = c[0]
    xs, ys = zip(*c)
    return [sum(xs) / len(xs), sum(ys) / len(ys)]


def main():
    if OUT.exists():
        OUT.unlink()
    db = sqlite3.connect(OUT)
    db.executescript("""
        CREATE TABLE ranges (id INTEGER PRIMARY KEY, street TEXT, fullname TEXT,
            lfrom INT, lto INT, rfrom INT, rto INT, zipl TEXT, zipr TEXT, coords TEXT);
        CREATE INDEX ranges_street ON ranges (street);
        CREATE VIRTUAL TABLE ranges_rt USING rtree (id, minx, maxx, miny, maxy);
        CREATE TABLE streets (id INTEGER PRIMARY KEY, street TEXT, fullname TEXT, zip TEXT,
            lon REAL, lat REAL, lo INT, hi INT, segs INT);
        CREATE VIRTUAL TABLE streets_fts USING fts5 (street, content='streets', content_rowid='id',
            tokenize='unicode61');
        CREATE TABLE addr (id INTEGER PRIMARY KEY, n INT, street TEXT, label TEXT, zip TEXT, lon REAL, lat REAL);
        CREATE INDEX addr_street ON addr (street, n);
        CREATE VIRTUAL TABLE addr_rt USING rtree (id, minx, maxx, miny, maxy);
        CREATE TABLE poi (id INTEGER PRIMARY KEY, name TEXT, kind TEXT, lon REAL, lat REAL, rank INT);
        CREATE VIRTUAL TABLE poi_fts USING fts5 (name, content='poi', content_rowid='id', tokenize='unicode61');
        CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
    """)

    # --- TIGER address ranges
    agg = {}
    n_rng = 0
    for f in tiger_features():
        p, g = f["properties"], f["geometry"]
        if not p.get("FULLNAME") or g is None or g["type"] != "LineString":
            continue
        coords = [[round(x, 6), round(y, 6)] for x, y in g["coordinates"]]
        xs, ys = zip(*coords)
        if max(xs) < BBOX[0] or min(xs) > BBOX[2] or max(ys) < BBOX[1] or min(ys) > BBOX[3]:
            continue
        lf, lt, rf, rt = (num(p.get(k)) for k in ("LFROMHN", "LTOHN", "RFROMHN", "RTOHN"))
        if lf is None and rf is None:
            continue
        street = norm_street(p["FULLNAME"])
        cur = db.execute("INSERT INTO ranges (street, fullname, lfrom, lto, rfrom, rto, zipl, zipr, coords) "
                         "VALUES (?,?,?,?,?,?,?,?,?)",
                         (street, p["FULLNAME"], lf, lt, rf, rt, p.get("ZIPL"), p.get("ZIPR"), json.dumps(coords)))
        db.execute("INSERT INTO ranges_rt VALUES (?,?,?,?,?)", (cur.lastrowid, min(xs), max(xs), min(ys), max(ys)))
        n_rng += 1
        for z in {p.get("ZIPL"), p.get("ZIPR")} - {None, ""}:
            a = agg.setdefault((street, z), {"full": p["FULLNAME"], "x": 0.0, "y": 0.0, "k": 0,
                                               "lo": 10 ** 9, "hi": 0})
            mx, my = coords[len(coords) // 2]
            a["x"] += mx
            a["y"] += my
            a["k"] += 1
            for v in (lf, lt, rf, rt):
                if v is not None:
                    a["lo"] = min(a["lo"], v)
                    a["hi"] = max(a["hi"], v)
    for (street, z), a in agg.items():
        cur = db.execute("INSERT INTO streets (street, fullname, zip, lon, lat, lo, hi, segs) VALUES (?,?,?,?,?,?,?,?)",
                         (street, a["full"], z, a["x"] / a["k"], a["y"] / a["k"], a["lo"], a["hi"], a["k"]))
        db.execute("INSERT INTO streets_fts (rowid, street) VALUES (?, ?)", (cur.lastrowid, street))

    # --- OSM exact addresses
    n_addr = 0
    for f in osm_features(["nwr/addr:housenumber"]):
        p = f["properties"]
        n, st = num(p.get("addr:housenumber")), p.get("addr:street")
        if n is None or not st:
            continue
        lon, lat = point_of(f["geometry"])
        street = norm_street(st)
        label = f"{p['addr:housenumber']} {st}"
        cur = db.execute("INSERT INTO addr (n, street, label, zip, lon, lat) VALUES (?,?,?,?,?,?)",
                         (n, street, label, (p.get("addr:postcode") or "")[:5], lon, lat))
        db.execute("INSERT INTO addr_rt VALUES (?,?,?,?,?)", (cur.lastrowid, lon, lon, lat, lat))
        n_addr += 1

    # --- OSM named places
    filters = [f"nwr/{k}={','.join(sorted(v))}" for k, v in POI_TAGS.items()]
    n_poi = 0
    seen = set()
    for f in osm_features(filters):
        p = f["properties"]
        name = p.get("name")
        kind = next((f"{k}={p[k]}" for k, v in POI_TAGS.items() if p.get(k) in v), None)
        if not name or not kind:
            continue
        lon, lat = point_of(f["geometry"])
        key = (name.lower(), round(lon, 3), round(lat, 3))
        if key in seen:
            continue
        seen.add(key)
        rank = 0 if kind.startswith(("place=", "aeroway", "amenity=university", "amenity=hospital")) else 1
        cur = db.execute("INSERT INTO poi (name, kind, lon, lat, rank) VALUES (?,?,?,?,?)", (name, kind, lon, lat, rank))
        db.execute("INSERT INTO poi_fts (rowid, name) VALUES (?, ?)", (cur.lastrowid, name))
        n_poi += 1

    db.execute("INSERT INTO meta VALUES ('sources', ?)", (
        "US Census TIGER/Line 2025 ADDRFEAT (" + ", ".join(z.name.split("_")[2] for z in TIGER) +
        "); OpenStreetMap addresses and named places (houston.osm.pbf)",))
    db.commit()
    db.execute("VACUUM")
    db.close()
    print(f"wrote {OUT} ({OUT.stat().st_size / 1e6:.0f} MB): {n_rng:,} TIGER ranges, {len(agg):,} street/ZIP "
          f"names, {n_addr:,} OSM addresses, {n_poi:,} named places")


if __name__ == "__main__":
    main()
