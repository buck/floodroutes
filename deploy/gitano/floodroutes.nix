# floodroutes public front end: imported by /etc/nixos/configuration.nix on gitano.
# Source of truth: ~/projects/floodroutes/deploy/gitano/floodroutes.nix (copied into /etc/nixos).
#
# gitano serves only built artifacts; the data pipeline (Postgres, 311 refresh,
# classification, geocoding, graph import) stays on quad. quad pushes updates with
# deploy/gitano/deploy.sh into /srv/floodroutes, which has the repo's layout:
#   api/ web/ risk/*.geojson,png,json data/osm/bayous.geojson
#   graphhopper/{graphhopper-web-11.0.jar, config.yml, custom_models/, graph-cache/}
#
# https://floodroutes.nbolt.com  (CNAME gitano; nginx + Let's Encrypt). Public since 2026-09-26.
# nginx in front: per-visitor rate limits, an OpenStreetMap tile cache (/tiles/), and access
# logs without query strings (visitors' addresses and GPS positions are never written to disk).
# To require a login again: set  basicAuthFile = "/var/lib/floodroutes/htpasswd";  on the
# virtual host (deploy.sh keeps that file current from quad's .env).
{ config, lib, pkgs, ... }:

let
  root = "/srv/floodroutes";
  apiPython = pkgs.python3.withPackages (ps: with ps; [ fastapi uvicorn httpx shapely pyproj pyyaml ]);
in
{
  users.groups.floodroutes = { };
  users.users.floodroutes = {
    isSystemUser = true;
    group = "floodroutes";
    home = root;
  };

  systemd.tmpfiles.rules = [
    "d ${root} 0755 floodroutes floodroutes -"
    "d /var/lib/floodroutes 0750 floodroutes nginx -"
  ];

  environment.systemPackages = [ pkgs.rsync ];  # deploy.sh pushes with rsync

  systemd.services.floodroutes-graphhopper = {
    description = "floodroutes: GraphHopper (car, car_flood) on 127.0.0.1:8989";
    after = [ "network.target" ];
    wantedBy = [ "multi-user.target" ];
    unitConfig.ConditionPathExists = "${root}/graphhopper/graph-cache/properties";
    serviceConfig = {
      Type = "simple";
      User = "floodroutes";
      WorkingDirectory = "${root}/graphhopper";
      ExecStart = "${pkgs.jdk21_headless}/bin/java -Xmx2g -jar graphhopper-web-11.0.jar server config.yml";
      Restart = "on-failure";
      RestartSec = "10s";
    };
  };

  systemd.services.floodroutes-api = {
    description = "floodroutes: route comparison API and map page on 127.0.0.1:8086";
    after = [ "floodroutes-graphhopper.service" "network.target" ];
    wants = [ "floodroutes-graphhopper.service" ];
    wantedBy = [ "multi-user.target" ];
    unitConfig.ConditionPathExists = "${root}/api/app.py";
    serviceConfig = {
      Type = "simple";
      User = "floodroutes";
      WorkingDirectory = root;
      # live flood reports from app users (api/live.py); outside /srv so deploys never touch it
      Environment = [ "LIVE_DB=/var/lib/floodroutes/live.sqlite" ];
      # 4 worker processes: one Python process only uses one core (~20 routes/s on gitano)
      # no access log: request lines carry visitors' addresses and coordinates
      ExecStart = "${apiPython}/bin/uvicorn api.app:app --host 127.0.0.1 --port 8086 --proxy-headers --workers 4 --no-access-log";
      Restart = "on-failure";
      RestartSec = "5s";
    };
  };

  security.acme = {
    acceptTerms = true;
    defaults.email = "buck@nbolt.com";
  };

  services.nginx = {
    enable = true;
    recommendedProxySettings = true;
    recommendedTlsSettings = true;
    recommendedGzipSettings = true;
    recommendedOptimisation = true;
    commonHttpConfig = ''
      # Privacy: log the path only (no query string) and only the /24 (or /48) of the address.
      map $remote_addr $floodroutes_anon_ip {
        ~^(?P<a>\d+\.\d+\.\d+)\.\d+$   $a.0;
        ~^(?P<a>[0-9a-fA-F]*:[0-9a-fA-F]*:[0-9a-fA-F]*):  $a::;
        default                            0.0.0.0;
      }
      log_format floodroutes_private '$floodroutes_anon_ip [$time_local] "$request_method $uri" $status $body_bytes_sent $request_time "$http_user_agent"';

      # Rate limits per visitor (real address, in memory only)
      limit_req_zone $binary_remote_addr zone=fr_route:10m rate=3r/s;
      limit_req_zone $binary_remote_addr zone=fr_search:10m rate=10r/s;
      limit_req_zone $binary_remote_addr zone=fr_report:10m rate=2r/s;
      limit_req_status 429;
      limit_req_log_level info;

      # OpenStreetMap tile cache: tiles are fetched from tile.openstreetmap.org only when a visitor
      # views them (no pre-seeding), kept as long as OSM's cache headers allow, served from disk after.
      proxy_cache_path /var/cache/nginx/osmtiles levels=1:2 keys_zone=osmtiles:50m max_size=20g
                       inactive=60d use_temp_path=off;
    '';
    virtualHosts."floodroutes.nbolt.com" = {
      enableACME = true;
      forceSSL = true;
      extraConfig = ''
        access_log /var/log/nginx/floodroutes.log floodroutes_private;
        error_log /var/log/nginx/floodroutes-error.log warn;
      '';
      locations."/" = {
        proxyPass = "http://127.0.0.1:8086";
      };
      locations."~ ^/(route|reverse)$" = {
        proxyPass = "http://127.0.0.1:8086";
        extraConfig = "limit_req zone=fr_route burst=20 nodelay;";
      };
      locations."= /reports" = {
        proxyPass = "http://127.0.0.1:8086";
        extraConfig = "limit_req zone=fr_report burst=10 nodelay;";
      };
      locations."= /geocode" = {
        proxyPass = "http://127.0.0.1:8086";
        extraConfig = "limit_req zone=fr_search burst=40 nodelay;";
      };
      locations."~ ^/tiles/(?<tz>1?[0-9])/(?<tx>[0-9]+)/(?<ty>[0-9]+)\\.png$" = {
        extraConfig = ''
          resolver 1.1.1.1 8.8.8.8 valid=300s ipv6=off;
          proxy_pass https://tile.openstreetmap.org/$tz/$tx/$ty.png;
          proxy_set_header Host tile.openstreetmap.org;
          proxy_set_header User-Agent "floodroutes/1.0 (+https://floodroutes.nbolt.com; buck@nbolt.com)";
          proxy_set_header Cookie "";
          proxy_ssl_server_name on;
          proxy_ssl_name tile.openstreetmap.org;
          proxy_cache osmtiles;
          proxy_cache_valid 200 7d;
          proxy_cache_valid 404 1h;
          proxy_cache_lock on;
          proxy_cache_use_stale error timeout updating http_500 http_502 http_503 http_504;
          add_header X-Tile-Cache $upstream_cache_status always;
          access_log off;
        '';
      };
    };
  };

  networking.firewall.allowedTCPPorts = [ 80 443 ];
}
