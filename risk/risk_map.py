#!/usr/bin/env python3
"""Draw the continuous risk surface (core.risk_hex scores) as a map.

Left:  raw H3 hex scores, log scale (what the tiers are cut from).
Right: the same scores smoothed with a Gaussian (~250 m) and drawn as a
       topographic contour map; the tier cut-offs (10 / 25 / 60 after
       smoothing) are the heavy contour lines.

Writes docs/risk_surface.png. Scores only; no case data.
"""
import sys
from pathlib import Path

import h3
import numpy as np
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
from db import connect  # noqa: E402

OUT = ROOT / "docs" / "risk_surface.png"
LON0, LON1, LAT0, LAT1 = -95.80, -95.05, 29.52, 30.12
STEP = 0.0008            # ~80 m grid
SIGMA_M = 250            # smoothing for the contour view
LANDMARKS = {"Downtown": (29.7604, -95.3698), "Meyerland": (29.6860, -95.4630),
             "Kingwood": (30.0500, -95.1850), "Clear Lake": (29.5600, -95.1100),
             "Memorial City": (29.7810, -95.5440), "Greenspoint": (29.9440, -95.4160)}


def gaussian_blur(a, sigma_px):
    """FFT Gaussian blur (numpy only)."""
    ny, nx = a.shape
    ky = np.fft.fftfreq(ny)[:, None]
    kx = np.fft.fftfreq(nx)[None, :]
    g = np.exp(-2 * (np.pi * sigma_px) ** 2 * (kx ** 2 + ky ** 2))
    return np.real(np.fft.ifft2(np.fft.fft2(a) * g))


def main():
    cfg = yaml.safe_load((ROOT / "risk" / "config.yaml").read_text())
    th = cfg["thresholds"]
    with connect() as conn:
        scores = dict(conn.execute("SELECT h3, score FROM core.risk_hex").fetchall())
    res = cfg["h3_resolution"]
    lons = np.arange(LON0, LON1, STEP)
    lats = np.arange(LAT0, LAT1, STEP)
    grid = np.zeros((len(lats), len(lons)))
    for i, la in enumerate(lats):
        for j, lo in enumerate(lons):
            grid[i, j] = scores.get(h3.latlng_to_cell(la, lo, res), 0.0)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LogNorm

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(18, 8.6), dpi=110)
    aspect = 1 / np.cos(np.radians(29.8))
    ext = (LON0, LON1, LAT0, LAT1)

    raw = np.ma.masked_less(grid, 1.0)
    im = ax1.imshow(raw, origin="lower", extent=ext, cmap="magma_r",
                    norm=LogNorm(vmin=1, vmax=max(500, float(grid.max()))), interpolation="nearest")
    ax1.set_title("Risk score per H3 hex (raw, log scale)")
    fig.colorbar(im, ax=ax1, shrink=0.8, label="hex score")

    px_m = STEP * 111_320 * np.cos(np.radians(29.8))
    smooth = gaussian_blur(grid, SIGMA_M / px_m)
    # rescale so a lone hex keeps roughly its own score scale at the peak
    smooth *= grid.sum() / max(smooth.sum(), 1e-9)
    levels = [1, 2, 4, 6, 10, 15, 25, 40, 60, 100, 160]
    z = np.ma.masked_less(smooth, 1)
    cf = ax2.contourf(lons, lats, z, levels=levels + [max(1000, smooth.max())], cmap="YlOrRd",
                      norm=LogNorm(vmin=1, vmax=160), extend="max")
    ax2.contour(lons, lats, smooth, levels=levels, colors="k", linewidths=0.25, alpha=0.5)
    cs = ax2.contour(lons, lats, smooth, levels=[th["low"], th["medium"], th["high"]],
                     colors=["#b45309", "#c2410c", "#7f1d1d"], linewidths=[0.9, 1.2, 1.6])
    ax2.clabel(cs, fmt={th["low"]: "low", th["medium"]: "med", th["high"]: "high"}, fontsize=7)
    ax2.set_title(f"Smoothed risk surface (~{SIGMA_M} m), contours; heavy lines = tier cut-offs")
    fig.colorbar(cf, ax=ax2, shrink=0.8, label="smoothed score")

    for ax in (ax1, ax2):
        ax.set_xlim(LON0, LON1)
        ax.set_ylim(LAT0, LAT1)
        ax.set_aspect(aspect)
        ax.set_facecolor("#f3f4f6")
        for name, (la, lo) in LANDMARKS.items():
            ax.plot(lo, la, "o", ms=3, color="#1d4ed8")
            ax.annotate(name, (lo, la), xytext=(4, 4), textcoords="offset points", fontsize=8, color="#1d4ed8")
        ax.set_xlabel("longitude")
        ax.set_ylabel("latitude")
    fig.suptitle("Houston flood-history risk surface from 311 reports, 2011–2026", fontsize=14)
    fig.tight_layout()
    fig.savefig(OUT)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
