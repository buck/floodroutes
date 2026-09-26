#!/usr/bin/env bash
# Push built floodroutes artifacts from quad to gitano and restart the services there.
# Only published outputs travel: the routing graph, risk/firefighter/bayou GeoJSON, the
# surface overlay, the web page and the API code. No database, no 311 text.
#
# Safe to rerun. The graph goes to graph-cache.incoming and is swapped in only after it
# arrived completely; GraphHopper is down for the few seconds of the swap and restart.
# Also (re)writes the basic-auth file from FLOODROUTES_WEB_USER/PASSWORD in .env.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/../.."
HOST=${DEPLOY_HOST:-gitano}
DEST=/srv/floodroutes

# 1. everything except the graph
rsync -a --delete --relative --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r \
  api/app.py api/__init__.py web/ \
  risk/flood_risk_tiers.geojson risk/hfd_locations.geojson \
  risk/risk_surface_overlay.png risk/risk_surface_overlay.json \
  data/osm/bayous.geojson \
  graphhopper/graphhopper-web-11.0.jar graphhopper/config.yml graphhopper/custom_models/ \
  "$HOST:$DEST/"

# 2. the graph, beside the live one
rsync -a --delete --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r \
  graphhopper/graph-cache/ "$HOST:$DEST/graphhopper/graph-cache.incoming/"

# 3. basic auth (hash only on the server; plain password stays in quad's .env)
user=$(sed -n 's/^FLOODROUTES_WEB_USER=//p' .env)
pass=$(sed -n 's/^FLOODROUTES_WEB_PASSWORD=//p' .env)
if [ -n "$user" ] && [ -n "$pass" ]; then
  line="$user:$(openssl passwd -apr1 "$pass")"
  ssh "$HOST" "install -d -m 0750 -g nginx /var/lib/floodroutes 2>/dev/null || install -d -m 0755 /var/lib/floodroutes
               umask 027; printf '%s\n' '$line' > /var/lib/floodroutes/htpasswd.new
               chgrp nginx /var/lib/floodroutes/htpasswd.new 2>/dev/null || chmod 0644 /var/lib/floodroutes/htpasswd.new
               mv /var/lib/floodroutes/htpasswd.new /var/lib/floodroutes/htpasswd"
fi

# 4. swap the graph and restart
ssh "$HOST" "set -e; cd $DEST/graphhopper
  chown -R floodroutes:floodroutes $DEST 2>/dev/null || true
  systemctl stop floodroutes-graphhopper 2>/dev/null || true
  rm -rf graph-cache.old; [ -d graph-cache ] && mv graph-cache graph-cache.old; mv graph-cache.incoming graph-cache
  rm -rf graph-cache.old
  systemctl start floodroutes-graphhopper 2>/dev/null || true
  for i in \$(seq 60); do curl -sf http://127.0.0.1:8989/health >/dev/null && break; sleep 2; done
  systemctl restart floodroutes-api 2>/dev/null || true"
echo "deployed to $HOST:$DEST"
