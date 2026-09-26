#!/usr/bin/env bash
# Clear or list live flood reports on the public site (gitano), from quad.
#
#   deploy/gitano/clear-reports.sh                 clear all reports (e.g. between demo rehearsals)
#   deploy/gitano/clear-reports.sh list            show active spots
#   deploy/gitano/clear-reports.sh near LAT LON [RADIUS_M]   clear reports near a point (default 150 m)
#
# Runs api.live_admin on gitano as the floodroutes user with the API's own Python.
set -euo pipefail
HOST=${DEPLOY_HOST:-gitano}
case "${1:-all}" in
  all)  args="clear-all" ;;
  list) args="list" ;;
  near) [ $# -ge 3 ] || { echo "usage: $0 near LAT LON [RADIUS_M]" >&2; exit 2; }
        args="clear-near $2 $3 ${4:-150}" ;;
  *)    sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
ssh "$HOST" "set -e; cd /srv/floodroutes
  py=\$(systemctl cat floodroutes-api | grep -o '/nix/store/[^ ]*/bin/uvicorn' | head -1 | sed 's|/uvicorn\$|/python|')
  sudo -u floodroutes env LIVE_DB=/var/lib/floodroutes/live.sqlite \"\$py\" -m api.live_admin $args"
