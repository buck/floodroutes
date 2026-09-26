#!/usr/bin/env bash
# Regenerate the risk surface, write the car_flood custom model and reimport the graph.
# Safe to rerun: every step rebuilds its output from scratch.
#   PYTHON / JAVA can be overridden (systemd passes Nix-store paths).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
PYTHON=${PYTHON:-.venv/bin/python}
JAVA=${JAVA:-java}
GH_HEAP=${GH_HEAP:-4g}
UNIT=floodroutes-graphhopper.service

echo "== $(date -Is) risk surface"
"$PYTHON" risk/build_risk.py
echo "== $(date -Is) custom model"
"$PYTHON" graphhopper/write_custom_model.py

managed=false
if systemctl list-unit-files "$UNIT" >/dev/null 2>&1 && systemctl is-enabled --quiet "$UNIT"; then
  managed=true
  sudo systemctl stop "$UNIT"
fi

# Import into a fresh directory, then swap, so a failed import keeps the old graph.
echo "== $(date -Is) graph import"
rm -rf graphhopper/graph-cache.new
(cd graphhopper && sed 's|graph.location: graph-cache$|graph.location: graph-cache.new|' config.yml > config.import.yml &&
  "$JAVA" -Xmx"$GH_HEAP" -jar graphhopper-web-11.0.jar import config.import.yml && rm config.import.yml)
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
