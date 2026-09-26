#!/usr/bin/env python3
"""Render the smoothed risk surface as a transparent PNG overlay for the web map.

Rows are spaced evenly in Web Mercator so Leaflet's imageOverlay lines up
with the street tiles. Writes risk/risk_surface_overlay.png and a JSON
sidecar with its bounds. Scores only; no case data.
"""
import datetime as dt
import json
import math
import sys
from pathlib import Path

import h3
import numpy as np
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
sys.path.insert(0, str(ROOT / "risk"))
from db import connect  # noqa: E402
from risk_map import gaussian_blur, load_bayous  # noqa: E402

PNG = ROOT / "risk" / "risk_surface_overlay.png"
META = ROOT / "risk" / "risk_surface_overlay.json"
LON0, LON1, LAT0, LAT1 = -95.80, -95.05, 29.52, 30.12
PX_M = 40                # output pixel size (metres)
SIGMA_M = 150            # street-scale smoothing
VMIN, VMAX = 1.0, 160.0  # log colour range (score)


def merc_y(lat):
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def inv_merc_y(y):
    return math.degrees(2 * math.atan(math.exp(y)) - math.pi / 2)


def main():
    cfg = yaml.safe_load((ROOT / "risk" / "config.yaml").read_text())
    res = cfg["h3_resolution"]
    with connect() as conn:
        scores = dict(conn.execute("SELECT h3, score FROM core.risk_hex").fetchall())

    coslat = math.cos(math.radians((LAT0 + LAT1) / 2))
    nx = int((LON1 - LON0) * 111_320 * coslat / PX_M)
    ny = int((LAT1 - LAT0) * 111_320 / PX_M)
    lons = np.linspace(LON0, LON1, nx)
    ys = np.linspace(merc_y(LAT0), merc_y(LAT1), ny)
    lats = np.array([inv_merc_y(y) for y in ys])
    grid = np.zeros((ny, nx))
    for i, la in enumerate(lats):
        for j, lo in enumerate(lons):
            grid[i, j] = scores.get(h3.latlng_to_cell(la, lo, res), 0.0)

    smooth = gaussian_blur(grid, SIGMA_M / PX_M)
    smooth *= grid.sum() / max(smooth.sum(), 1e-9)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    t = np.clip((np.log10(np.maximum(smooth, 1e-9)) - math.log10(VMIN)) /
                (math.log10(VMAX) - math.log10(VMIN)), 0, 1)
    rgba = plt.get_cmap("YlOrRd")(t)
    rgba[..., 3] = np.where(smooth < VMIN, 0.0, 0.25 + 0.55 * t)  # transparent below the lowest level
    plt.imsave(PNG, np.flipud(rgba))  # image row 0 = north
    META.write_text(json.dumps({
        "bounds": [[LAT0, LON0], [LAT1, LON1]],
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "pixel_m": PX_M, "sigma_m": SIGMA_M, "score_range": [VMIN, VMAX],
        "colormap": "YlOrRd (log)",
    }))
    load_bayous()  # make sure data/osm/bayous.geojson exists for the map's bayou layer
    print(f"wrote {PNG} ({nx}x{ny}, {PNG.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
