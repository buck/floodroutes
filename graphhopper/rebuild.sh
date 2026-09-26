#!/usr/bin/env bash
# Regenerate the risk surface, write the car_flood custom model and reimport the graph.
# Safe to rerun: every step rebuilds its output from scratch.
#   PYTHON / JAVA can be overridden (systemd passes Nix-store paths).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
PYTHON=${PYTHON:-.venv/bin/python}
JAVA=${JAVA:-java}
JDK_BIN=$(dirname "$(readlink -f "$(command -v "$JAVA")")")
GH_HEAP=${GH_HEAP:-4g}
UNIT=floodroutes-graphhopper.service

echo "== $(date -Is) risk surface"
"$PYTHON" risk/build_risk.py
"$PYTHON" risk/surface_overlay.py
echo "== $(date -Is) custom model"
"$PYTHON" graphhopper/write_custom_model.py

# Build the flood_risk import plugin when its sources changed (needs a JDK: javac, jar).
PLUGIN=graphhopper/plugin/floodroutes-gh.jar
if [ ! -f "$PLUGIN" ] || [ -n "$(find graphhopper/plugin/src -newer "$PLUGIN" -name '*.java')" ]; then
  echo "== $(date -Is) build plugin"
  rm -rf graphhopper/plugin/classes && mkdir -p graphhopper/plugin/classes
  "$JDK_BIN/javac" -cp graphhopper/graphhopper-web-11.0.jar -d graphhopper/plugin/classes graphhopper/plugin/src/floodroutes/*.java
  "$JDK_BIN/jar" cf "$PLUGIN" -C graphhopper/plugin/classes .
fi

managed=false
if systemctl list-unit-files "$UNIT" >/dev/null 2>&1 && systemctl is-enabled --quiet "$UNIT"; then
  managed=true
  sudo systemctl stop "$UNIT"
fi

# Import into a fresh directory, then swap, so a failed import keeps the old graph.
echo "== $(date -Is) graph import"
rm -rf graphhopper/graph-cache.new
(cd graphhopper && sed 's|graph.location: graph-cache$|graph.location: graph-cache.new|' config.yml > config.import.yml &&
  "$JAVA" -Xmx"$GH_HEAP" -cp graphhopper-web-11.0.jar:plugin/floodroutes-gh.jar \
    floodroutes.FloodImport config.import.yml ../risk/flood_risk_tiers.geojson && rm config.import.yml)
rm -rf graphhopper/graph-cache.old
[ -d graphhopper/graph-cache ] && mv graphhopper/graph-cache graphhopper/graph-cache.old
mv graphhopper/graph-cache.new graphhopper/graph-cache
rm -rf graphhopper/graph-cache.old

if $managed; then
  sudo systemctl start "$UNIT"
  for i in $(seq 60); do curl -sf http://127.0.0.1:8989/health >/dev/null && break; sleep 2; done
  sudo systemctl restart floodroutes-api.service || true
fi
echo "== $(date -Is) done"
