#!/usr/bin/env python3
"""Zoomed view of the White Oak / Little White Oak / Buffalo Bayou wedges:
flood-signal locations per major storm event versus dry days.

Writes docs/wedge_events.png (points only, no case data).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
from db import connect  # noqa: E402

OUT = ROOT / "docs" / "wedge_events.png"
BAYOUS = ROOT / "data" / "osm" / "bayous.geojson"  # written by risk/risk_map.py
LON0, LON1, LAT0, LAT1 = -95.49, -95.33, 29.74, 29.87
EVENTS = ["Memorial Day flood", "Tax Day flood", "Harvey (incl. late reports)", "Imelda (incl. late reports)"]
LANDMARKS = {"Downtown": (29.7604, -95.3698), "Heights": (29.7980, -95.3980),
             "I-10 / T.C. Jester": (29.7790, -95.4290), "Independence Hts": (29.8200, -95.4000)}


def main():
    with connect() as conn:
        ev = dict(conn.execute("SELECT label, event_id FROM core.storm_event WHERE label = ANY(%s)", (EVENTS,)).fetchall())
        q = """SELECT f.tier, ST_X(r.geom), ST_Y(r.geom) FROM core.flood_signal f
               JOIN core.service_request r USING (case_number)
               WHERE r.geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326) AND {cond}"""
        box = (LON0, LAT0, LON1, LAT1)
        panels = []
        for label in EVENTS:
            eid = ev.get(label)
            rows = conn.execute(q.format(cond="f.event_id = %s"), (*box, eid)).fetchall() if eid else []
            panels.append((label.replace(" (incl. late reports)", ""), rows))
        dry = conn.execute(q.format(cond="NOT f.event_day AND f.tier <> 'weak'"), box).fetchall()
        panels.append(("All dry days, 2011–2026 (strong + moderate)", dry))

    bayous = {}
    for f in json.loads(BAYOUS.read_text())["features"]:
        bayous.setdefault(f["properties"]["name"], []).append(f["geometry"]["coordinates"])

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    fig, axes = plt.subplots(2, 3, figsize=(18, 11), dpi=110)
    axes = axes.ravel()
    colors = {"strong": "#b91c1c", "moderate": "#f59e0b", "weak": "#9ca3af"}
    for ax, (title, rows) in zip(axes, panels):
        for name, parts in bayous.items():
            for coords in parts:
                xs, ys = zip(*coords)
                ax.plot(xs, ys, color="#0369a1", lw=1.6, alpha=0.8)
        for tier in ("weak", "moderate", "strong"):
            pts = [(x, y) for t, x, y in rows if t == tier]
            if pts:
                xs, ys = zip(*pts)
                ax.scatter(xs, ys, s=6 if len(rows) < 3000 else 2, c=colors[tier], alpha=0.75, linewidths=0,
                           label=f"{tier} ({len(pts):,})")
        for name, (la, lo) in LANDMARKS.items():
            ax.plot(lo, la, "s", ms=3, color="#111827")
            ax.annotate(name, (lo, la), xytext=(3, 3), textcoords="offset points", fontsize=7)
        ax.set_title(f"{title}: {len(rows):,} reports", fontsize=10)
        ax.set_xlim(LON0, LON1)
        ax.set_ylim(LAT0, LAT1)
        ax.set_aspect(1 / np.cos(np.radians(29.8)))
        ax.set_facecolor("#f9fafb")
        ax.tick_params(labelsize=7)
        ax.legend(loc="lower left", fontsize=7, markerscale=3, frameon=False)
    # bayou legend panel
    ax = axes[-1]
    ax.axis("off")
    ax.text(0.02, 0.95, "Blue: bayou channels (OSM)\n  White Oak (centre, W→E to downtown)\n"
            "  Little White Oak (from the north)\n  Buffalo (south, W→E)\n\n"
            "Points: 311 flood signals located in this box.\n311 records where residents call from,\n"
            "so the bayou banks themselves are empty\nby construction, even when under water.",
            va="top", fontsize=10, transform=ax.transAxes)
    fig.suptitle("White Oak / Little White Oak / Buffalo Bayou wedges: reports by storm event", fontsize=14)
    fig.tight_layout()
    fig.savefig(OUT)
    print(f"wrote {OUT}; " + ", ".join(f"{t}: {len(r)}" for t, r in panels))


if __name__ == "__main__":
    main()
