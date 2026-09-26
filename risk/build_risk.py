#!/usr/bin/env python3
"""Phase 4: flood-signal points -> H3 hex scores -> dissolved tier polygons.

Rebuilds core.risk_hex and core.risk_polygon in one transaction and writes
risk/flood_risk_tiers.geojson. The GeoJSON carries only tier, score and
aggregate counts: no case numbers, text or addresses.

Firefighter-reported locations (risk/hfd_locations.geojson, from the 2018 HFD
survey comments) set a minimum score on the hexes they touch; the final hex
score is max(311 score, firefighter minimum).

Usage: build_risk.py
"""
import collections
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

import h3
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
from db import connect  # noqa: E402

CONFIG = Path(__file__).resolve().parent / "config.yaml"
GEOJSON = Path(__file__).resolve().parent / "flood_risk_tiers.geojson"
TIERS = ("low", "medium", "high")

DDL = """
CREATE TABLE IF NOT EXISTS core.risk_hex (
    h3          text PRIMARY KEY,
    geom        geometry(Polygon, 4326) NOT NULL,
    score       real NOT NULL,
    tier        text CHECK (tier IN ('low', 'medium', 'high')),
    cases       integer NOT NULL,
    strong      integer NOT NULL,
    moderate    integer NOT NULL,
    weak        integer NOT NULL,
    storm_events integer[] NOT NULL,
    first_day   date,
    last_day    date,
    config_sha  text NOT NULL
);
CREATE INDEX IF NOT EXISTS risk_hex_geom ON core.risk_hex USING gist (geom);
CREATE TABLE IF NOT EXISTS core.risk_polygon (
    id          text PRIMARY KEY,           -- e.g. high_001
    tier        text NOT NULL,
    geom        geometry(Polygon, 4326) NOT NULL,
    score_max   real NOT NULL,
    score_mean  real NOT NULL,
    hexes       integer NOT NULL,
    cases       integer NOT NULL,
    storm_events integer NOT NULL,
    first_day   date,
    last_day    date,
    config_sha  text NOT NULL
);
CREATE INDEX IF NOT EXISTS risk_polygon_geom ON core.risk_polygon USING gist (geom);
ALTER TABLE core.risk_hex ADD COLUMN IF NOT EXISTS score_311 real;
ALTER TABLE core.risk_hex ADD COLUMN IF NOT EXISTS hfd_floor real;
ALTER TABLE core.risk_hex ADD COLUMN IF NOT EXISTS hfd_refs text[];
ALTER TABLE core.risk_polygon ADD COLUMN IF NOT EXISTS hfd_refs text[];
"""

DISSOLVE = """
INSERT INTO core.risk_polygon (id, tier, geom, score_max, score_mean, hexes, cases, storm_events,
                               first_day, last_day, config_sha, hfd_refs)
WITH parts AS (
    SELECT tier, (ST_Dump(ST_Union(geom))).geom AS g FROM core.risk_hex
    WHERE tier IS NOT NULL GROUP BY tier
), agg AS (
    SELECT p.tier, p.g,
           max(h.score) smax, avg(h.score) smean, count(*) nhex, sum(h.cases) ncases,
           (SELECT count(DISTINCT e) FROM core.risk_hex h2, unnest(h2.storm_events) e
             WHERE h2.tier = p.tier AND ST_Intersects(h2.geom, p.g)
               AND ST_Area(ST_Intersection(h2.geom, p.g)) > 0) nevents,
           min(h.first_day) d0, max(h.last_day) d1,
           (SELECT array_agg(DISTINCT r ORDER BY r) FROM core.risk_hex h3x, unnest(h3x.hfd_refs) r
             WHERE h3x.tier = p.tier AND ST_Intersects(h3x.geom, p.g)
               AND ST_Area(ST_Intersection(h3x.geom, p.g)) > 0) hrefs
    FROM parts p JOIN core.risk_hex h
      ON h.tier = p.tier AND ST_Intersects(h.geom, p.g) AND ST_Area(ST_Intersection(h.geom, p.g)) > 0
    GROUP BY p.tier, p.g
    HAVING count(*) >= %(min_hexes)s
)
SELECT tier || '_' || lpad(row_number() OVER (PARTITION BY tier ORDER BY smax DESC)::text, 4, '0'),
       tier,
       ST_Transform(ST_SimplifyPreserveTopology(ST_Transform(g, 2278), %(tol_ft)s), 4326),
       smax, smean, nhex, ncases, nevents, d0, d1, %(sha)s, hrefs
FROM agg
"""


def hfd_hexes(cfg, res):
    """hex -> (minimum score, [comment ids]) from firefighter-reported locations."""
    hc = cfg.get("hfd_comments") or {}
    path = ROOT / hc.get("file", "risk/hfd_locations.geojson")
    if not hc.get("enabled") or not path.exists():
        return {}
    from shapely.geometry import shape
    step = hc.get("sample_m", 15) / 111_320  # degrees, ~15 m
    out = {}
    for f in json.loads(path.read_text())["features"]:
        floor = hc["min_score"][f["properties"]["severity"]]
        g = shape(f["geometry"])
        if g.geom_type == "Point":
            r = hc.get("point_radius_m", 60) / 111_320
            pts = [(g.y, g.x)] + [(g.y + r * dy, g.x + r * dx / 0.867)
                                  for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (.7, .7), (-.7, .7), (.7, -.7), (-.7, -.7))]
        else:
            lines = g.geoms if hasattr(g, "geoms") else [g]
            pts = []
            for ln in lines:
                n = max(2, int(ln.length / step) + 1)
                pts += [(p.y, p.x) for p in (ln.interpolate(i / (n - 1), normalized=True) for i in range(n))]
        for lat, lon in pts:
            hx = h3.latlng_to_cell(lat, lon, res)
            fl, refs = out.get(hx, (0.0, set()))
            out[hx] = (max(fl, floor), refs | {f["properties"]["id"]})
    return out


def main() -> int:
    raw = CONFIG.read_bytes()
    cfg = yaml.safe_load(raw)
    sha = hashlib.sha256(raw).hexdigest()
    res = cfg["h3_resolution"]
    as_of = dt.date.fromisoformat(cfg["as_of"]) if cfg.get("as_of") else dt.date.today()
    hl = cfg["half_life_years"]
    wts = cfg["tier_weights"]
    cap = cfg["max_signals_per_hex_day"]
    th = cfg["thresholds"]

    with connect() as conn:
        conn.execute(DDL)
        rows = conn.execute("""
            SELECT f.tier, f.created_day, f.event_id, ST_Y(r.geom), ST_X(r.geom)
            FROM core.flood_signal f JOIN core.service_request r USING (case_number)
            WHERE r.geom IS NOT NULL
            ORDER BY f.created_day, f.case_number""").fetchall()

        per_day = collections.Counter()
        acc = collections.defaultdict(lambda: {"s": 0.0, "n": 0, "strong": 0, "moderate": 0, "weak": 0,
                                               "ev": set(), "d0": None, "d1": None})
        capped = 0
        for tier, day, ev, lat, lon in rows:
            hx = h3.latlng_to_cell(lat, lon, res)
            if day is not None:
                per_day[(hx, day)] += 1
                if per_day[(hx, day)] > cap:
                    capped += 1
                    continue
            age = (as_of - day).days / 365.25 if day else 15
            a = acc[hx]
            a["s"] += wts[tier] * 0.5 ** (max(age, 0) / hl)
            a["n"] += 1
            a[tier] += 1
            if ev:
                a["ev"].add(ev)
            if day:
                a["d0"] = min(a["d0"] or day, day)
                a["d1"] = max(a["d1"] or day, day)

        hfd = hfd_hexes(cfg, res)
        empty = {"s": 0.0, "n": 0, "strong": 0, "moderate": 0, "weak": 0, "ev": set(), "d0": None, "d1": None}
        hex_rows = []
        for hx in set(acc) | set(hfd):
            a = acc.get(hx, empty)
            score_311 = a["s"] * (1 + cfg["recurrence"]["per_event"] * len(a["ev"]))
            floor, refs = hfd.get(hx, (0.0, set()))
            score = max(score_311, floor)
            tier = None
            for t in reversed(TIERS):
                if score >= th[t]:
                    tier = t
                    break
            ring = [(lng, lat) for lat, lng in h3.cell_to_boundary(hx)]
            wkt = "POLYGON((" + ",".join(f"{x} {y}" for x, y in ring + ring[:1]) + "))"
            hex_rows.append((hx, wkt, score, tier, a["n"], a["strong"], a["moderate"], a["weak"],
                             sorted(a["ev"]), a["d0"], a["d1"], sha, score_311, floor or None,
                             sorted(refs) or None))

        with conn.transaction():
            conn.execute("TRUNCATE core.risk_hex, core.risk_polygon")
            with conn.cursor().copy("""COPY core.risk_hex (h3, geom, score, tier, cases, strong, moderate, weak,
                                       storm_events, first_day, last_day, config_sha,
                                       score_311, hfd_floor, hfd_refs) FROM STDIN""") as cp:
                for r in hex_rows:
                    cp.write_row((r[0], f"SRID=4326;{r[1]}", *r[2:]))
            conn.execute(DISSOLVE, {"min_hexes": cfg["min_hexes_per_polygon"],
                                    "tol_ft": cfg["simplify_m"] / 0.3048006096, "sha": sha})

        summary = conn.execute("""
            SELECT tier, count(*), sum(hexes), sum(cases), round((sum(ST_Area(geom::geography)) / 1e6)::numeric, 2),
                   max(ST_NPoints(geom)), sum(ST_NPoints(geom))
            FROM core.risk_polygon GROUP BY tier
            ORDER BY array_position(ARRAY['high','medium','low'], tier)""").fetchall()
        feats = conn.execute("""
            SELECT id, tier, round(score_max::numeric, 1), round(score_mean::numeric, 1), hexes, cases,
                   storm_events, first_day::text, last_day::text, ST_AsGeoJSON(geom, 6), hfd_refs
            FROM core.risk_polygon ORDER BY array_position(ARRAY['high','medium','low'], tier), id""").fetchall()

    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": f[0],
         "properties": {"tier": f[1], "score_max": float(f[2]), "score_mean": float(f[3]), "hexes": f[4],
                        "cases": f[5], "storm_events": f[6], "first_day": f[7], "last_day": f[8],
                        **({"firefighter_reports": f[10]} if f[10] else {})},
         "geometry": json.loads(f[9])} for f in feats]}
    GEOJSON.write_text(json.dumps(fc, separators=(",", ":")))
    print(f"{len(rows):,} located signals ({capped:,} capped as same-hex same-day duplicates) -> "
          f"{len(hex_rows):,} hexes; tiers: " +
          "; ".join(f"{t}: {n} polygons / {h} hexes / {c} cases / {a} km2 / max {mp} pts / total {tp} pts"
                    for t, n, h, c, a, mp, tp in summary))
    raised = sum(1 for r in hex_rows if r[13] and r[13] > r[12])
    print(f"firefighter reports: {len(hfd):,} hexes touched, {raised:,} raised above their 311 score")
    print(f"wrote {GEOJSON} ({GEOJSON.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
