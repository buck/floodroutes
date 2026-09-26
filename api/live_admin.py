#!/usr/bin/env python3
"""Inspect or clear live flood reports.

  python -m api.live_admin list
  python -m api.live_admin clear-all
  python -m api.live_admin clear-near LAT LON [RADIUS_M=150]

Uses LIVE_DB (default data/live.sqlite; on gitano /var/lib/floodroutes/live.sqlite).
On gitano: cd /srv/floodroutes && sudo -u floodroutes LIVE_DB=/var/lib/floodroutes/live.sqlite \\
           <api python> -m api.live_admin list   (see docs/PARAMETERS.md)
"""
import os
import sys
from pathlib import Path

from .live import LiveStore

ROOT = Path(__file__).resolve().parent.parent


def main(argv):
    store = LiveStore(Path(os.getenv("LIVE_DB", str(ROOT / "data" / "live.sqlite"))))
    cmd = argv[0] if argv else "list"
    if cmd == "list":
        spots = store.spots()
        for s in spots:
            print(f"{s['state']:10s} {s['lat']:.5f},{s['lon']:.5f}  {s['reports']} reports / {s['devices']} devices  "
                  f"{s['age_min']} min ago, expires in {s['expires_min']} min")
        print(f"{len(spots)} active spot(s)")
    elif cmd == "clear-all":
        print(f"deleted {store.clear()} report(s)")
    elif cmd == "clear-near":
        lat, lon = float(argv[1]), float(argv[2])
        radius = float(argv[3]) if len(argv) > 3 else 150
        print(f"deleted {store.clear(lat, lon, radius)} report(s)")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
