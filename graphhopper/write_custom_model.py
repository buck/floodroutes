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
    mode = cfg.get("graphhopper_area_mode", "encoded_value")
    if mode == "encoded_value":
        # flood_risk (0 none, 1 low, 2 medium, 3 high) is set per edge at import by
        # graphhopper/plugin (FloodImport); no geometry is evaluated at query time.
        level = {"high": 3, "medium": 2, "low": 1}
        stmts = [{("if" if i == 0 else "else_if"): f"flood_risk == {level[t]}", "multiply_by": str(prio[t])}
                 for i, t in enumerate(TIERS)]
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({"priority": stmts}, indent=1))
        print(f"wrote {OUT} (encoded_value: flood_risk priority rules, no areas)")
        return 0
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
