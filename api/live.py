"""Live flood reports from app users: storage, trust rules, and routing areas.

A report is a road point (the reporter's GPS snapped to the nearest road), a kind
(water | impassable | clear), a time and a hashed device token. Nothing else about
the reporter is stored: no trail, no IP address, no raw device token.

Reports within cluster_radius_m form a "spot". A spot's state comes from the distinct
devices reporting it (see live_config.yaml):
  closed    impassable from >= close_min_devices devices within close_window_min
  cleared   "water's gone" from >= clear_min_devices devices after the last hazard report
  disputed  a hazard with one "water's gone" after it
  impassable / water   otherwise
Each active spot becomes a small area in the flood-aware route's custom model.
"""
import hashlib
import math
import sqlite3
import time
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent / "live_config.yaml"
M_PER_DEG = 111_320
KINDS = {"water", "impassable", "clear"}


def load_config(path: Path = CONFIG_PATH) -> dict:
    return yaml.safe_load(path.read_text())


def metres(lat1, lon1, lat2, lon2):
    return math.hypot((lon2 - lon1) * M_PER_DEG * math.cos(math.radians((lat1 + lat2) / 2)),
                      (lat2 - lat1) * M_PER_DEG)


def device_hash(token: str) -> str:
    return hashlib.sha256(("floodroutes:" + token).encode()).hexdigest()[:16]


class LiveStore:
    def __init__(self, path: Path, cfg: dict | None = None):
        self.path = Path(path)
        self.cfg = cfg or load_config()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS reports (
                    id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL,
                    lat REAL NOT NULL, lon REAL NOT NULL, device TEXT NOT NULL, accuracy REAL);
                CREATE INDEX IF NOT EXISTS reports_ts ON reports (ts);
            """)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA busy_timeout = 5000")
        return db

    # ---- writing
    def device_recent(self, dev: str, now: float) -> int:
        with self._db() as db:
            return db.execute("SELECT count(*) FROM reports WHERE device = ? AND ts > ?",
                              (dev, now - self.cfg["per_device_window_min"] * 60)).fetchone()[0]

    def add(self, kind: str, lat: float, lon: float, token: str, accuracy: float, now: float | None = None):
        now = now or time.time()
        dev = device_hash(token)
        with self._db() as db:
            db.execute("INSERT INTO reports (ts, kind, lat, lon, device, accuracy) VALUES (?,?,?,?,?,?)",
                       (now, kind, lat, lon, dev, accuracy))
        return dev

    # ---- reading
    def _recent(self, now: float):
        horizon = now - max(self.cfg["expire_water_min"], self.cfg["expire_impassable_min"],
                            self.cfg["expire_clear_min"]) * 60
        with self._db() as db:
            return db.execute("SELECT ts, kind, lat, lon, device FROM reports WHERE ts > ? ORDER BY ts",
                              (horizon,)).fetchall()

    def spots(self, now: float | None = None) -> list[dict]:
        c = self.cfg
        now = now or time.time()
        expire = {"water": c["expire_water_min"] * 60, "impassable": c["expire_impassable_min"] * 60,
                  "clear": c["expire_clear_min"] * 60}
        live = [r for r in self._recent(now) if now - r[0] <= expire[r[1]]]
        # greedy clustering around hazard reports (clears only attach to existing spots)
        clusters = []
        for ts, kind, lat, lon, dev in live:
            if kind == "clear":
                continue
            for cl in clusters:
                if metres(lat, lon, cl["lat"], cl["lon"]) <= c["cluster_radius_m"]:
                    cl["reports"].append((ts, kind, lat, lon, dev))
                    n = len(cl["reports"])
                    cl["lat"] += (lat - cl["lat"]) / n
                    cl["lon"] += (lon - cl["lon"]) / n
                    break
            else:
                clusters.append({"lat": lat, "lon": lon, "reports": [(ts, kind, lat, lon, dev)]})
        out = []
        for cl in clusters:
            hz = cl["reports"]
            last_hazard = max(r[0] for r in hz)
            clears = {dev for ts, kind, lat, lon, dev in live
                      if kind == "clear" and ts > last_hazard
                      and metres(lat, lon, cl["lat"], cl["lon"]) <= c["cluster_radius_m"]}
            imp = [r for r in hz if r[1] == "impassable"]
            imp_devs = {r[4] for r in imp}
            closed = False
            if len(imp_devs) >= c["close_min_devices"]:
                # enough distinct devices inside one close_window_min window?
                times = sorted((r[0], r[4]) for r in imp)
                for i, (t0, _) in enumerate(times):
                    devs = {d for t, d in times[i:] if t - t0 <= c["close_window_min"] * 60}
                    if len(devs) >= c["close_min_devices"]:
                        closed = True
                        break
            if len(clears) >= c["clear_min_devices"]:
                continue  # cleared
            if clears:
                state, prio = "disputed", c["priority_disputed"]
            elif closed:
                state, prio = "closed", c["priority_closed"]
            elif imp:
                state, prio = "impassable", c["priority_impassable_single"]
            else:
                state, prio = "water", c["priority_water"]
            expires = max(r[0] + expire[r[1]] for r in hz)
            out.append({"lat": cl["lat"], "lon": cl["lon"], "state": state, "priority": prio,
                        "reports": len(hz), "devices": len({r[4] for r in hz}),
                        "age_min": round((now - last_hazard) / 60), "expires_min": round((expires - now) / 60)})
        out.sort(key=lambda s: (s["priority"], -s["devices"]))
        return out[:c["max_active_spots"]]

    # ---- routing
    def custom_model(self, now: float | None = None) -> dict | None:
        """Areas and priority rules for the flood-aware route, or None when nothing is active."""
        spots = self.spots(now)
        if not spots:
            return None
        r = self.cfg["area_radius_m"]
        feats, by_prio = [], {}
        for i, s in enumerate(spots, 1):
            aid = f"report_{i:03d}"
            dlat = r / M_PER_DEG
            dlon = r / (M_PER_DEG * math.cos(math.radians(s["lat"])))
            ring = [[round(s["lon"] + dlon * math.cos(a), 6), round(s["lat"] + dlat * math.sin(a), 6)]
                    for a in (2 * math.pi * k / 12 for k in range(12))]
            feats.append({"type": "Feature", "id": aid, "properties": {},
                          "geometry": {"type": "Polygon", "coordinates": [ring + [ring[0]]]}})
            by_prio.setdefault(s["priority"], []).append(f"in_{aid}")
        stmts = [{("if" if i == 0 else "else_if"): " || ".join(ids), "multiply_by": str(p)}
                 for i, (p, ids) in enumerate(sorted(by_prio.items()))]
        return {"priority": stmts, "areas": {"type": "FeatureCollection", "features": feats}}

    # ---- admin
    def clear(self, lat=None, lon=None, radius_m=None) -> int:
        with self._db() as db:
            if lat is None:
                return db.execute("DELETE FROM reports").rowcount
            rows = db.execute("SELECT id, lat, lon FROM reports").fetchall()
            ids = [i for i, a, b in rows if metres(lat, lon, a, b) <= radius_m]
            db.executemany("DELETE FROM reports WHERE id = ?", [(i,) for i in ids])
            return len(ids)
