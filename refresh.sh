#!/usr/bin/env bash
# Daily refresh: re-download the MTD and current-year YTD extracts and upsert them.
# Safe to rerun: unchanged files are skipped by download.py, loaded files by load.py,
# and core upserts only accept rows from an equal-or-newer extract.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
PYTHON=${PYTHON:-.venv/bin/python}

on_fail() { [ -x /usr/local/bin/alert ] && /usr/local/bin/alert "floodroutes refresh failed (line $1)"; }
trap 'on_fail $LINENO' ERR

year=$(date +%Y)
pat="MTD|compressed-${year}"
# the previous year's YTD file keeps changing for a while after New Year
[ "$(date +%m)" = 01 ] && pat="${pat}|compressed-$((year - 1))"

echo "== $(date -Is) refresh (${pat})"
"$PYTHON" ingest/download.py --only "$pat" --keep 2
"$PYTHON" ingest/load.py --only "$pat"
"$PYTHON" classify/classify.py
"$PYTHON" geocode/geocode.py --priority 0 --no-report
# The risk surface and graph are rebuilt separately: graphhopper/rebuild.sh
echo "== $(date -Is) done"
