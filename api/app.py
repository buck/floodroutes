"""floodroutes API: baseline vs flood-aware routes from the local GraphHopper.

GET  /route?from=lat,lon&to=lat,lon
POST /route  {"from": [lat, lon], "to": [lat, lon], "areas": <GeoJSON FeatureCollection>?}
     `areas` are hard exclusions applied to the flood-aware route only (hook
     for live high-water reports; no live sources are wired up yet).
GET  /risk.geojson   the risk-tier polygons (tier and aggregate counts only)
GET  /risk_surface.png, /risk_surface.json   smoothed risk-surface overlay + bounds
GET  /bayous.geojson  major bayou channels (OSM)
GET  /hfd.geojson     firefighter-reported flooding locations (2018 HFD survey, published report)
POST /reports       {"lat","lon","accuracy","kind": water|impassable|clear,"device"}: a live flood report
GET  /reports        active report spots (GeoJSON; no reporter data)
GET  /geocode?q=     address / place / street / "A & B" search (self-hosted: Census TIGER + OSM)
GET  /reverse?lat=&lon=  nearest address label for a map point
GET  /               the map page

Responses carry geometry, distances, times and risk-polygon aggregates only:
no case numbers, addresses or resident text exist anywhere in this service.
"""
import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from pyproj import Transformer
from shapely.geometry import LineString, Point, shape
from shapely.ops import transform
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parent.parent
RISK = ROOT / "risk" / "flood_risk_tiers.geojson"
SURFACE_PNG = ROOT / "risk" / "risk_surface_overlay.png"
SURFACE_META = ROOT / "risk" / "risk_surface_overlay.json"
BAYOUS = ROOT / "data" / "osm" / "bayous.geojson"
HFD = ROOT / "risk" / "hfd_locations.geojson"
GEOCODER_DB = ROOT / "data" / "geocoder.sqlite"
WEB = ROOT / "web" / "index.html"
ABOUT = ROOT / "web" / "about.html"
GH = os.getenv("GRAPHHOPPER_URL", "http://127.0.0.1:8989")
BBOX = (-96.2, 29.2, -94.7, 30.4)  # graph extent
TIER_ORDER = {"high": 0, "medium": 1, "low": 2}
MAX_LIVE_AREAS = 200
LIVE_DB = Path(os.getenv("LIVE_DB", str(ROOT / "data" / "live.sqlite")))

to_ft = Transformer.from_crs("EPSG:4326", "EPSG:2278", always_xy=True).transform

# One HTTP client for the app's lifetime: building an httpx client per request
# costs ~200 ms (it loads an SSL context even for plain http).
_client: httpx.AsyncClient | None = None
_client_loop = None


def gh_client() -> httpx.AsyncClient:
    """One client per event loop (in production: one for the app's lifetime)."""
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client_loop is not loop:
        _client = httpx.AsyncClient(timeout=30, limits=httpx.Limits(max_keepalive_connections=20))
        _client_loop = loop
    return _client


@asynccontextmanager
async def lifespan(app):
    yield
    if _client is not None:
        await _client.aclose()


app = FastAPI(title="floodroutes", docs_url="/docs", redoc_url=None, lifespan=lifespan)


class RiskIndex:
    """Risk polygons in memory with an STRtree; reloads when the GeoJSON changes."""

    def __init__(self):
        self.mtime = None
        self._load()

    def _load(self):
        fc = json.loads(RISK.read_text())
        self.raw = fc
        # project every polygon to feet once here, not on every request
        self.feats = [(f["id"], f["properties"], g, transform(to_ft, g))
                      for f in fc["features"] for g in [shape(f["geometry"])]]
        self.tree = STRtree([g for _, _, g, _ in self.feats])
        self.mtime = RISK.stat().st_mtime

    def fresh(self):
        if RISK.stat().st_mtime != self.mtime:
            self._load()
        return self

    def crossed(self, line: LineString) -> list[dict]:
        line_ft = transform(to_ft, line)
        out = []
        for i in self.tree.query(line, predicate="intersects"):
            fid, props, _, geom_ft = self.feats[i]
            inside = geom_ft.intersection(line_ft)
            inside_m = inside.length * 0.3048006096
            parts = getattr(inside, "geoms", [inside])
            starts = [line_ft.project(Point(g.coords[0])) for g in parts if not g.is_empty and len(g.coords)]
            along_m = round(min(starts) * 0.3048006096) if starts else None
            out.append({"id": fid, "tier": props["tier"], "score_max": props["score_max"],
                        "storm_events": props["storm_events"], "reports": props["cases"],
                        "first_year": (props.get("first_day") or "")[:4] or None,
                        "last_year": (props.get("last_day") or "")[:4] or None,
                        "meters_inside": round(inside_m), "along_m": along_m})
        out.sort(key=lambda d: (TIER_ORDER[d["tier"]], -d["score_max"]))
        return out


risk = RiskIndex()


def parse_point(s: str) -> tuple[float, float]:
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", s or "")
    if not m:
        raise HTTPException(422, "points must be 'lat,lon'")
    lat, lon = float(m.group(1)), float(m.group(2))
    return check_point(lat, lon)


def check_point(lat: float, lon: float) -> tuple[float, float]:
    if not (BBOX[1] <= lat <= BBOX[3] and BBOX[0] <= lon <= BBOX[2]):
        raise HTTPException(422, "point is outside the Greater Houston routing area")
    return lat, lon


async def gh_route(client, a, b, profile, custom_model=None):
    body = {"points": [[a[1], a[0]], [b[1], b[0]]], "profile": profile,
            "points_encoded": False, "instructions": False, "calc_points": True}
    if profile != "car" or custom_model:
        body["ch.disable"] = True  # car_flood has LM only; custom models need non-CH routing
    if custom_model:
        body["custom_model"] = custom_model
    r = await client.post(f"{GH}/route", json=body)
    if r.status_code != 200:
        msg = r.json().get("message", r.text) if r.headers.get("content-type", "").startswith("application/json") else r.text
        raise HTTPException(502 if r.status_code >= 500 else 422, f"GraphHopper ({profile}): {msg[:300]}")
    p = r.json()["paths"][0]
    coords = p["points"]["coordinates"]
    return {"distance_m": round(p["distance"]), "time_s": round(p["time"] / 1000),
            "geometry": {"type": "LineString", "coordinates": coords}}


def live_model(areas: dict | None) -> dict | None:
    """Turn a GeoJSON FeatureCollection into hard exclusions (priority 0)."""
    if not areas:
        return None
    feats = areas.get("features") if isinstance(areas, dict) else None
    if not isinstance(feats, list) or not feats:
        raise HTTPException(422, "areas must be a GeoJSON FeatureCollection with features")
    if len(feats) > MAX_LIVE_AREAS:
        raise HTTPException(422, f"at most {MAX_LIVE_AREAS} live areas")
    out, ids = [], []
    for i, f in enumerate(feats, 1):
        g = (f or {}).get("geometry") or {}
        if g.get("type") not in ("Polygon", "MultiPolygon"):
            raise HTTPException(422, f"area {i}: geometry must be Polygon or MultiPolygon")
        aid = f"live_{i:03d}"
        out.append({"type": "Feature", "id": aid, "properties": {}, "geometry": g})
        ids.append(f"in_{aid}")
    return {"priority": [{"if": " || ".join(ids), "multiply_by": "0"}],
            "areas": {"type": "FeatureCollection", "features": out}}


_live = None


def live_store():
    """User flood reports (api/live.py); created on first use so imports stay cheap."""
    global _live
    if _live is None:
        from .live import LiveStore
        _live = LiveStore(LIVE_DB)
    return _live


def merge_models(*models):
    """Stack custom models: each one's priority rules stay a separate if-block."""
    models = [m for m in models if m]
    if not models:
        return None
    return {"priority": [st for m in models for st in m["priority"]],
            "areas": {"type": "FeatureCollection",
                      "features": [f for m in models for f in m["areas"]["features"]]}}


async def compare(a, b, areas=None):
    request_model = live_model(areas)
    reports_model = live_store().custom_model()
    cm = merge_models(request_model, reports_model)
    client = gh_client()
    base, flood = await asyncio.gather(gh_route(client, a, b, "car"),
                                       gh_route(client, a, b, "car_flood", cm))
    idx = risk.fresh()
    base_line = LineString(base["geometry"]["coordinates"])
    flood_line = LineString(flood["geometry"]["coordinates"])
    base["hotspots_crossed"] = idx.crossed(base_line)
    flood["hotspots_crossed"] = idx.crossed(flood_line)
    flood_ids = {h["id"] for h in flood["hotspots_crossed"]}
    avoided = [h for h in base["hotspots_crossed"] if h["id"] not in flood_ids]

    def exposure(hs):
        return {t: sum(h["meters_inside"] for h in hs if h["tier"] == t) for t in TIER_ORDER}

    d0, t0 = max(base["distance_m"], 1), max(base["time_s"], 1)
    return {
        "baseline": base,
        "flood_aware": flood,
        "detour": {
            "added_m": flood["distance_m"] - base["distance_m"],
            "added_s": flood["time_s"] - base["time_s"],
            "added_distance_pct": round(100 * (flood["distance_m"] - base["distance_m"]) / d0, 1),
            "added_time_pct": round(100 * (flood["time_s"] - base["time_s"]) / t0, 1),
        },
        "risk_exposure_m": {"baseline": exposure(base["hotspots_crossed"]),
                            "flood_aware": exposure(flood["hotspots_crossed"])},
        "hotspots_avoided": avoided,
        "live_exclusions": len(request_model["areas"]["features"]) if request_model else 0,
        "live_reports_used": len(reports_model["areas"]["features"]) if reports_model else 0,
        "disclaimer": "Avoids streets with a history of flooding. Absence of reports does not mean "
                      "a street is dry. Turn around, don't drown.",
    }


def fast_json(obj) -> Response:
    """Serialize with json.dumps directly; FastAPI's jsonable_encoder costs ~12 ms per route."""
    return Response(json.dumps(obj, separators=(",", ":")), media_type="application/json")


@app.get("/route")
async def route_get(from_: str = Query(..., alias="from"), to: str = Query(...)):
    return fast_json(await compare(parse_point(from_), parse_point(to)))


@app.post("/route")
async def route_post(body: dict = Body(...)):
    try:
        a = check_point(float(body["from"][0]), float(body["from"][1]))
        b = check_point(float(body["to"][0]), float(body["to"][1]))
    except (KeyError, TypeError, ValueError, IndexError):
        raise HTTPException(422, "body needs 'from' and 'to' as [lat, lon]")
    return fast_json(await compare(a, b, body.get("areas")))


@app.post("/reports")
async def post_report(body: dict = Body(...)):
    from .live import KINDS
    cfg = live_store().cfg
    try:
        lat, lon = float(body["lat"]), float(body["lon"])
        accuracy = float(body.get("accuracy") or 0)
        kind, token = str(body["kind"]), str(body["device"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(422, "body needs lat, lon, accuracy, kind and device")
    check_point(lat, lon)
    if kind not in KINDS:
        raise HTTPException(422, f"kind must be one of {sorted(KINDS)}")
    if not (8 <= len(token) <= 100):
        raise HTTPException(422, "device token must be 8-100 characters")
    if accuracy <= 0 or accuracy > cfg["max_accuracy_m"]:
        raise HTTPException(422, f"GPS accuracy must be within {cfg['max_accuracy_m']} m to report")
    import time as _t
    from .live import device_hash, metres
    if live_store().device_recent(device_hash(token), _t.time()) >= cfg["per_device_max"]:
        raise HTTPException(429, "too many reports from this device; try again in a few minutes")
    r = await gh_client().get(f"{GH}/nearest", params={"point": f"{lat},{lon}", "profile": "car"})
    if r.status_code != 200:
        raise HTTPException(502, "could not find the nearest road")
    rlon, rlat = r.json()["coordinates"][:2]
    if metres(lat, lon, rlat, rlon) > cfg["snap_max_m"]:
        raise HTTPException(422, "not on or near a road")
    live_store().add(kind, rlat, rlon, token, accuracy)
    spot = next((sp for sp in live_store().spots() if metres(rlat, rlon, sp["lat"], sp["lon"]) <= cfg["cluster_radius_m"]), None)
    return {"ok": True, "road_point": [rlat, rlon], "spot": spot}


@app.get("/reports")
def get_reports():
    return JSONResponse({"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(sp["lon"], 6), round(sp["lat"], 6)]},
         "properties": {k: sp[k] for k in ("state", "reports", "devices", "age_min", "expires_min")}}
        for sp in live_store().spots()]}, headers={"Cache-Control": "no-store"})


@app.get("/risk.geojson")
def risk_geojson():
    return JSONResponse(risk.fresh().raw, headers={"Cache-Control": "max-age=300"})


@app.get("/risk_surface.png")
def risk_surface_png():
    if not SURFACE_PNG.exists():
        raise HTTPException(404, "run risk/surface_overlay.py")
    return FileResponse(SURFACE_PNG, media_type="image/png", headers={"Cache-Control": "max-age=300"})


@app.get("/risk_surface.json")
def risk_surface_meta():
    if not SURFACE_META.exists():
        raise HTTPException(404, "run risk/surface_overlay.py")
    return JSONResponse(json.loads(SURFACE_META.read_text()), headers={"Cache-Control": "max-age=300"})


@app.get("/bayous.geojson")
def bayous():
    if not BAYOUS.exists():
        raise HTTPException(404, "run risk/surface_overlay.py")
    return JSONResponse(json.loads(BAYOUS.read_text()), headers={"Cache-Control": "max-age=3600"})


@app.get("/hfd.geojson")
def hfd():
    if not HFD.exists():
        raise HTTPException(404, "run risk/hfd_locations.py")
    return JSONResponse(json.loads(HFD.read_text()), headers={"Cache-Control": "max-age=300"})


_geocoder = None


def geocoder():
    global _geocoder
    if _geocoder is None:
        if not GEOCODER_DB.exists():
            raise HTTPException(503, "address search not built (geocode/build_address_index.py)")
        from .geocoder import Geocoder
        _geocoder = Geocoder(GEOCODER_DB)
    return _geocoder


@app.get("/geocode")
def geocode(q: str = Query(..., min_length=1, max_length=120), limit: int = Query(6, ge=1, le=10)):
    return {"results": geocoder().search(q, limit)}


@app.get("/reverse")
def reverse(lat: float, lon: float):
    check_point(lat, lon)
    return geocoder().reverse(lat, lon)


@app.get("/health")
async def health():
    gh = (await gh_client().get(f"{GH}/health", timeout=5)).text.strip()
    return {"api": "ok", "graphhopper": gh, "risk_polygons": len(risk.fresh().feats)}


@app.get("/")
def index():
    return FileResponse(WEB)


@app.get("/about")
def about():
    return FileResponse(ABOUT)
