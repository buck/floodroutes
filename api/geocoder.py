"""Self-hosted address search over data/geocoder.sqlite (see geocode/build_address_index.py).

search("4800 n braes")  -> interpolated / exact addresses, streets, named places, "A & B" intersections
reverse(lat, lon)       -> "4815 N Braeswood Blvd, 77096" (exact OSM point if within ~25 m,
                           else interpolated from the TIGER block range, else "near <street>")
All data is public (Census TIGER, OpenStreetMap) and nothing is sent to third parties.
"""
import json
import math
import re
import sqlite3
from pathlib import Path

try:
    from .addrnorm import norm_street
except ImportError:  # imported as a top-level module by the index builder
    from addrnorm import norm_street

COS = math.cos(math.radians(29.8))
M_PER_DEG = 111_320


class Geocoder:
    def __init__(self, path: Path):
        self.path = path
        self.db = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True, check_same_thread=False)

    # ---- helpers
    @staticmethod
    def _fts(q: str) -> str | None:
        toks = re.findall(r"[A-Z0-9]+", q.upper())
        if not toks:
            return None
        return " ".join(f'"{t}"' for t in toks[:-1]) + f' "{toks[-1]}"*'

    @staticmethod
    def _along(coords, frac):
        seg = [math.hypot((x2 - x1) * COS, y2 - y1) for (x1, y1), (x2, y2) in zip(coords, coords[1:])]
        total = sum(seg) or 1e-12
        target = frac * total
        for (x1, y1), (x2, y2), s in zip(coords, coords[1:], seg):
            if target <= s or s == seg[-1]:
                t = 0 if s == 0 else min(1, target / s)
                return x1 + t * (x2 - x1), y1 + t * (y2 - y1)
            target -= s
        return coords[-1]

    def _interpolate(self, n, street, zip_=None):
        best = None
        for lf, lt, rf, rt, zl, zr, coords in self.db.execute(
                "SELECT lfrom, lto, rfrom, rto, zipl, zipr, coords FROM ranges WHERE street = ?", (street,)):
            for a, b, z in ((lf, lt, zl), (rf, rt, zr)):
                if a is None or b is None or not (min(a, b) <= n <= max(a, b)) or a % 2 != n % 2:
                    continue
                if zip_ and z != zip_:
                    continue
                frac = 0.5 if a == b else (n - a) / (b - a)
                lon, lat = self._along(json.loads(coords), frac)
                best = (lat, lon, z)
                break
            if best:
                break
        return best

    # ---- forward
    def search(self, q: str, limit: int = 6):
        q = (q or "").strip()
        if len(q) < 2:
            return []
        zm = re.search(r"\b(77\d{3}|78\d{3})\b", q)
        zip_ = zm.group(1) if zm else None
        q = re.sub(r",?\s*(houston|tx|texas|usa)\b", " ", q, flags=re.I)
        q = re.sub(r"\b(77\d{3}|78\d{3})\b", " ", q).strip(" ,")
        parts = re.split(r"\s*(?:&|@|\band\b|\bat\b)\s*", q, flags=re.I)
        if len(parts) == 2 and all(parts) and not re.match(r"^\d", parts[0]):
            hit = self.intersection(parts[0], parts[1])
            if hit:
                return [hit]
        m = re.match(r"^(\d+)\s+(.+)$", q)
        out = []
        if m:
            n, rest = int(m.group(1)), norm_street(m.group(2))
            fts = self._fts(rest)
            if not fts:
                return []
            rows = self.db.execute(
                "SELECT s.street, s.fullname, s.zip FROM streets_fts f JOIN streets s ON s.id = f.rowid "
                "WHERE streets_fts MATCH ? AND s.lo <= ? AND s.hi >= ? ORDER BY bm25(streets_fts), s.segs DESC LIMIT 40",
                (fts, n, n)).fetchall()
            seen = set()
            for street, full, z in rows:
                if zip_ and z != zip_:
                    continue
                if (street, z) in seen:
                    continue
                seen.add((street, z))
                hit = self._interpolate(n, street, z)
                if not hit:
                    continue
                # prefer an exact OSM point for this number on this street, if it is near this ZIP's block
                ex = min(((math.hypot((alon - hit[1]) * COS, alat - hit[0]) * M_PER_DEG, alat, alon)
                          for alat, alon in self.db.execute(
                              "SELECT lat, lon FROM addr WHERE street = ? AND n = ?", (street, n))),
                         default=None)
                if ex and ex[0] < 500:
                    out.append({"label": f"{n} {full}, {hit[2]}", "lat": ex[1], "lon": ex[2], "kind": "address",
                                "precision": "exact"})
                else:
                    out.append({"label": f"{n} {full}, {hit[2]}", "lat": hit[0], "lon": hit[1],
                                "kind": "address", "precision": "interpolated"})
                if len(out) >= limit:
                    break
            return out
        fts = self._fts(norm_street(q))
        if not fts:
            return []
        places = [{"label": name, "lat": lat, "lon": lon, "kind": "place",
                   "detail": kind.split("=")[1].replace("_", " ")}
                  for name, kind, lat, lon in self.db.execute(
                      "SELECT p.name, p.kind, p.lat, p.lon FROM poi_fts f JOIN poi p ON p.id = f.rowid "
                      "WHERE poi_fts MATCH ? ORDER BY p.rank, bm25(poi_fts) LIMIT ?", (self._fts(q), limit))]
        streets, per_name = [], {}
        for full, z, lat, lon in self.db.execute(
                "SELECT s.fullname, s.zip, s.lat, s.lon FROM streets_fts f JOIN streets s ON s.id = f.rowid "
                "WHERE streets_fts MATCH ? " + ("AND s.zip = ? " if zip_ else "") +
                "ORDER BY bm25(streets_fts), s.segs DESC LIMIT 60",
                (fts, zip_) if zip_ else (fts,)):
            if per_name.get(full, 0) >= 2:  # the same street in at most two ZIPs
                continue
            per_name[full] = per_name.get(full, 0) + 1
            streets.append({"label": f"{full}, {z}", "lat": lat, "lon": lon, "kind": "street"})
        if re.search(r"\b(st|ave|blvd|dr|rd|ln|pkwy|fwy|hwy|ct|cir|pl|trl|way)\.?$", q, re.I):
            out = streets + places          # looks like a street
        else:
            out = places[:2] + streets + places[2:]
        return out[:limit]

    def intersection(self, a: str, b: str):
        from shapely.geometry import LineString
        from shapely.strtree import STRtree

        def lines(name):
            fts = self._fts(norm_street(name))
            if not fts:
                return {}, []
            names = [r[0] for r in self.db.execute(
                "SELECT DISTINCT s.street FROM streets_fts f JOIN streets s ON s.id = f.rowid "
                "WHERE streets_fts MATCH ? ORDER BY bm25(streets_fts) LIMIT 3", (fts,))]
            full, geoms = {}, []
            for st in names:
                for fn, coords in self.db.execute("SELECT fullname, coords FROM ranges WHERE street = ?", (st,)):
                    full[len(geoms)] = fn
                    geoms.append(LineString(json.loads(coords)))
            return full, geoms

        fa, la = lines(a)
        fb, lb = lines(b)
        if not la or not lb:
            return None
        tree = STRtree(lb)
        for i, g in enumerate(la):
            for j in tree.query(g, predicate="intersects"):
                p = g.intersection(lb[j])
                p = p if p.geom_type == "Point" else p.centroid
                return {"label": f"{fa[i]} & {fb[int(j)]}", "lat": p.y, "lon": p.x, "kind": "intersection"}
        return None

    # ---- reverse
    def reverse(self, lat: float, lon: float):
        d = 0.0003
        best = None
        for label, zip_, alat, alon in self.db.execute(
                "SELECT a.label, a.zip, a.lat, a.lon FROM addr_rt r JOIN addr a ON a.id = r.id "
                "WHERE r.minx >= ? AND r.maxx <= ? AND r.miny >= ? AND r.maxy <= ?",
                (lon - d, lon + d, lat - d, lat + d)):
            dist = math.hypot((alon - lon) * COS, alat - lat) * M_PER_DEG
            if dist <= 25 and (best is None or dist < best[0]):
                best = (dist, f"{label}{', ' + zip_ if zip_ else ''}")
        if best:
            return {"label": best[1], "lat": lat, "lon": lon, "kind": "address", "precision": "exact"}
        d = 0.005
        near = None
        for rid, full, lf, lt, rf, rt, zl, zr, coords in self.db.execute(
                "SELECT g.id, g.fullname, g.lfrom, g.lto, g.rfrom, g.rto, g.zipl, g.zipr, g.coords "
                "FROM ranges_rt r JOIN ranges g ON g.id = r.id "
                "WHERE r.maxx >= ? AND r.minx <= ? AND r.maxy >= ? AND r.miny <= ?",
                (lon - d, lon + d, lat - d, lat + d)):
            pts = json.loads(coords)
            walked, total = 0.0, 0.0
            seg_len = [math.hypot((x2 - x1) * COS, y2 - y1) for (x1, y1), (x2, y2) in zip(pts, pts[1:])]
            total = sum(seg_len) or 1e-12
            for (x1, y1), (x2, y2), s in zip(pts, pts[1:], seg_len):
                dx, dy = (x2 - x1) * COS, y2 - y1
                px, py = (lon - x1) * COS, lat - y1
                t = 0 if s == 0 else max(0, min(1, (px * dx + py * dy) / (s * s)))
                dist = math.hypot(px - t * dx, py - t * dy) * M_PER_DEG
                if near is None or dist < near[0]:
                    side_left = (dx * py - dy * px) > 0
                    near = (dist, full, (walked + t * s) / total, side_left, (lf, lt, zl), (rf, rt, zr))
                walked += s
        if near is None or near[0] > 150:
            d = 0.004
            poi = min(((math.hypot((plon - lon) * COS, plat - lat) * M_PER_DEG, name)
                       for name, plat, plon in self.db.execute(
                           "SELECT name, lat, lon FROM poi WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ? "
                           "AND kind NOT LIKE 'place=%'", (lon - d, lon + d, lat - d, lat + d))), default=None)
            if poi and poi[0] <= 400:
                return {"label": f"near {poi[1]}", "lat": lat, "lon": lon, "kind": "place"}
            if near is not None and near[0] <= 500:
                return {"label": f"near {near[1]}", "lat": lat, "lon": lon, "kind": "street"}
            return {"label": f"{lat:.5f}, {lon:.5f}", "lat": lat, "lon": lon, "kind": "point"}
        dist, full, frac, left, L, R = near
        for a, b, z in ((L, R) if left else (R, L)):
            if a is not None and b is not None:
                n = round(a + frac * (b - a))
                if n % 2 != a % 2:
                    n += 1 if b >= a else -1
                return {"label": f"{n} {full}, {z}", "lat": lat, "lon": lon, "kind": "address",
                        "precision": "interpolated"}
        return {"label": f"near {full}", "lat": lat, "lon": lon, "kind": "street"}
