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
