"""API tests. Need GraphHopper on :8989 (they call the real router).

Run: python -m pytest tests/ -q
"""
import json
from pathlib import Path

from fastapi.testclient import TestClient
from shapely.geometry import LineString, box, shape

from api.app import app

client = TestClient(app)
A, B = "29.7690,-95.4245", "29.7930,-95.4245"   # crosses a known high-risk area
FIXTURE = Path(__file__).parent / "fixtures" / "live_areas_example.geojson"
FORBIDDEN_KEYS = {"case_number", "description", "address", "incident_address", "notes", "title"}


def keys(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from keys(v)
    elif isinstance(o, list):
        for v in o:
            yield from keys(v)


def test_get_route_shape_and_privacy():
    r = client.get("/route", params={"from": A, "to": B})
    assert r.status_code == 200
    d = r.json()
    assert d["baseline"]["distance_m"] > 0 and d["flood_aware"]["distance_m"] > 0
    assert d["risk_exposure_m"]["flood_aware"]["high"] <= d["risk_exposure_m"]["baseline"]["high"]
    assert not FORBIDDEN_KEYS & set(keys(d))


def test_post_live_area_is_hard_exclusion():
    base = client.get("/route", params={"from": A, "to": B}).json()
    line = LineString(base["flood_aware"]["geometry"]["coordinates"])
    mid = line.interpolate(0.5, normalized=True)
    blocker = box(mid.x - 0.0008, mid.y - 0.0008, mid.x + 0.0008, mid.y + 0.0008)
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": json.loads(json.dumps(blocker.__geo_interface__))}]}
    r = client.post("/route", json={"from": [29.7690, -95.4245], "to": [29.7930, -95.4245], "areas": fc})
    assert r.status_code == 200
    d = r.json()
    assert d["live_exclusions"] == 1
    assert not LineString(d["flood_aware"]["geometry"]["coordinates"]).intersects(blocker.buffer(-0.0002))
    # the baseline is never affected by live areas
    assert d["baseline"]["distance_m"] == base["baseline"]["distance_m"]


def test_post_fixture_file_accepted():
    fc = json.loads(FIXTURE.read_text())
    r = client.post("/route", json={"from": [29.7690, -95.4245], "to": [29.7930, -95.4245], "areas": fc})
    assert r.status_code == 200
    line = LineString(r.json()["flood_aware"]["geometry"]["coordinates"])
    assert not line.intersects(shape(fc["features"][0]["geometry"]).buffer(-0.0002))


def test_rejects_points_outside_area():
    assert client.get("/route", params={"from": "40.7,-74.0", "to": B}).status_code == 422


def test_risk_geojson_has_no_case_data():
    fc = client.get("/risk.geojson").json()
    assert fc["features"]
    assert not FORBIDDEN_KEYS & set(keys(fc))


# --- layers added after the first test round -------------------------------------------

HOUSTON = (-96.2, 29.2, -94.7, 30.4)  # routing bbox: lon0, lat0, lon1, lat1
HFD_PROPS = {"id", "station", "kind", "severity", "quote", "length_m"}


def coords(g):
    """All [lon, lat] pairs of a GeoJSON geometry, at any nesting depth."""
    def walk(c):
        if isinstance(c[0], (int, float)):
            yield c
        else:
            for part in c:
                yield from walk(part)
    return list(walk(g["coordinates"]))


def in_houston(lon, lat):
    return HOUSTON[0] <= lon <= HOUSTON[2] and HOUSTON[1] <= lat <= HOUSTON[3]


def test_hfd_geojson_is_published_report_fields_only():
    r = client.get("/hfd.geojson")
    assert r.status_code == 200
    fc = r.json()
    assert "Measuring Flooding with the Houston Fire Department" in fc.get("source", "")
    assert fc["features"]
    for f in fc["features"]:
        assert set(f["properties"]) <= HFD_PROPS, f["properties"].keys()
        assert f["properties"]["severity"] in {"cut_off", "recurring", "major_event"}
        assert f["properties"]["kind"] in {"intersection", "segment", "station_front"}
        assert f["geometry"]["type"] in {"Point", "LineString", "MultiLineString"}
        assert all(in_houston(lon, lat) for lon, lat in coords(f["geometry"]))


def test_risk_areas_reference_real_firefighter_reports():
    hfd_ids = {f["properties"]["id"] for f in client.get("/hfd.geojson").json()["features"]}
    refs = [ref for f in client.get("/risk.geojson").json()["features"]
            for ref in f["properties"].get("firefighter_reports", [])]
    assert refs, "no risk area carries a firefighter report"
    assert set(refs) <= hfd_ids


def test_risk_surface_overlay():
    meta = client.get("/risk_surface.json")
    assert meta.status_code == 200
    m = meta.json()
    (lat0, lon0), (lat1, lon1) = m["bounds"]
    assert lat0 < lat1 and lon0 < lon1
    assert in_houston(lon0, lat0) and in_houston(lon1, lat1)
    assert m["generated"]
    png = client.get("/risk_surface.png")
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    body = png.content
    assert body[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = int.from_bytes(body[16:20], "big"), int.from_bytes(body[20:24], "big")
    assert width > 500 and height > 500


def test_bayous_geojson():
    r = client.get("/bayous.geojson")
    assert r.status_code == 200
    feats = r.json()["features"]
    names = {f["properties"]["name"] for f in feats}
    assert {"Brays Bayou", "Buffalo Bayou", "White Oak Bayou"} <= names
    for f in feats:
        assert set(f["properties"]) == {"name"}
        assert f["geometry"]["type"] == "LineString"


def test_map_page():
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    html = r.text
    assert ("Avoids streets with a history of flooding. Absence of reports does not mean a street is dry. "
            "Turn around, don't drown.") in html
    for endpoint in ("route?", "risk.geojson", "risk_surface.json", "bayous.geojson", "hfd.geojson"):
        assert endpoint in html, endpoint
    # third-party code only from the allowed CDN
    for src in __import__("re").findall(r'<script[^>]+src="([^"]+)"', html):
        assert src.startswith("https://cdnjs.cloudflare.com/"), src


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    d = r.json()
    assert d["api"] == "ok" and d["graphhopper"] == "OK" and d["risk_polygons"] > 0


def test_api_source_never_touches_resident_text():
    """Static guard: the API must not read 311 free-text columns or the case tables at all."""
    api = Path(__file__).resolve().parent.parent / "api"
    src = "".join((api / f).read_text() for f in ("app.py", "geocoder.py", "addrnorm.py", "live.py"))
    for forbidden in ("description", "latest_notes", "resolution_notes", "conflict_notes",
                      "service_request", "flood_signal", "incident_address", "psycopg"):
        assert forbidden not in src, forbidden


# --- self-hosted address search -------------------------------------------------------

def test_geocode_address_place_intersection():
    r = client.get("/geocode", params={"q": "4800 n braeswood"})
    assert r.status_code == 200
    top = r.json()["results"][0]
    assert top["label"].startswith("4800 N Braeswood Blvd") and top["kind"] == "address"
    assert in_houston(top["lon"], top["lat"])
    places = client.get("/geocode", params={"q": "rice university"}).json()["results"]
    assert any(p["kind"] == "place" and p["label"] == "Rice University" for p in places)
    inter = client.get("/geocode", params={"q": "braeswood & stella link"}).json()["results"]
    assert inter and inter[0]["kind"] == "intersection" and "Stella Link" in inter[0]["label"]


def test_reverse_geocode():
    r = client.get("/reverse", params={"lat": 29.681555, "lon": -95.460975})
    assert r.status_code == 200 and "Braeswood" in r.json()["label"]
    assert client.get("/reverse", params={"lat": 40.7, "lon": -74.0}).status_code == 422


def test_about_page():
    r = client.get("/about")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "Not for emergency use" in r.text and "911" in r.text
    assert 'href="about"' in client.get("/").text


# --- live flood reports ---------------------------------------------------------------

import pytest


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A throwaway report store so tests never touch data/live.sqlite."""
    import api.app as appmod
    from api.live import LiveStore
    store = LiveStore(tmp_path / "live.sqlite")
    monkeypatch.setattr(appmod, "_live", store)
    return store


def report(kind, lat, lon, device, accuracy=10):
    return client.post("/reports", json={"lat": lat, "lon": lon, "accuracy": accuracy, "kind": kind, "device": device})


def test_report_accepted_and_listed_without_reporter_data(live):
    r = report("water", 29.7800, -95.4245, "device-test-A")
    assert r.status_code == 200 and r.json()["spot"]["state"] == "water"
    fc = client.get("/reports").json()
    assert len(fc["features"]) == 1
    assert set(fc["features"][0]["properties"]) == {"state", "reports", "devices", "age_min", "expires_min"}


def test_two_devices_close_a_road_for_the_flood_route_only(live):
    base = client.get("/route", params={"from": A, "to": B}).json()
    line = LineString(base["flood_aware"]["geometry"]["coordinates"])
    mid = line.interpolate(0.5, normalized=True)
    assert report("impassable", mid.y, mid.x, "device-test-A").json()["spot"]["state"] == "impassable"
    assert report("impassable", mid.y, mid.x, "device-test-B").json()["spot"]["state"] == "closed"
    d = client.get("/route", params={"from": A, "to": B}).json()
    assert d["live_reports_used"] == 1
    road = client.get("/reports").json()["features"][0]["geometry"]["coordinates"]
    spot = __import__("shapely.geometry", fromlist=["Point"]).Point(road).buffer(0.0003)  # ~30 m, inside the 60 m area
    assert not LineString(d["flood_aware"]["geometry"]["coordinates"]).intersects(spot)
    assert d["baseline"]["distance_m"] == base["baseline"]["distance_m"]


def test_report_validation_and_device_limit(live):
    assert report("water", 29.78, -95.4245, "device-test-A", accuracy=500).status_code == 422
    assert report("flood", 29.78, -95.4245, "device-test-A").status_code == 422
    assert report("water", 40.7, -74.0, "device-test-A").status_code == 422
    for _ in range(6):
        assert report("water", 29.78, -95.4245, "device-test-C").status_code == 200
    assert report("water", 29.78, -95.4245, "device-test-C").status_code == 429
