#!/usr/bin/env python3
"""Write graphhopper/custom_models/flood_risk.json from risk/flood_risk_tiers.geojson.

The model only lowers priority inside risk areas (never excludes a road);
car.json supplies the base car behaviour. Settings come from risk/config.yaml.
"""
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
GEOJSON = ROOT / "risk" / "flood_risk_tiers.geojson"
OUT = ROOT / "graphhopper" / "custom_models" / "flood_risk.json"
TIERS = ("high", "medium", "low")


def main() -> int:
    cfg = yaml.safe_load((ROOT / "risk" / "config.yaml").read_text())
    prio = cfg["graphhopper_priority"]
    mode = cfg.get("graphhopper_area_mode", "multipolygon")
    fc = json.loads(GEOJSON.read_text())
    areas, conds = [], {}
    for tier in TIERS:
        feats = [f for f in fc["features"] if f["properties"]["tier"] == tier]
        if not feats:
            continue
        if mode == "multipolygon":
            coords = []
            for f in feats:
                g = f["geometry"]
                coords += [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
            areas.append({"type": "Feature", "id": f"flood_{tier}",
                          "properties": {"tier": tier, "polygons": len(feats)},
                          "geometry": {"type": "MultiPolygon", "coordinates": coords}})
            conds[tier] = f"in_flood_{tier}"
        else:
            for f in feats:
                areas.append({"type": "Feature", "id": f["id"], "properties": {"tier": tier},
                              "geometry": f["geometry"]})
            conds[tier] = " || ".join(f"in_{f['id']}" for f in feats)
    stmts = []
    for tier in TIERS:
        if tier in conds:
            stmts.append({("if" if not stmts else "else_if"): conds[tier], "multiply_by": str(prio[tier])})
    model = {"priority": stmts, "areas": {"type": "FeatureCollection", "features": areas}}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(model, separators=(",", ":")))
    print(f"wrote {OUT} ({mode}: {len(areas)} areas, {OUT.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
