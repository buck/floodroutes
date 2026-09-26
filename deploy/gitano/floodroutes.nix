# floodroutes public front end: imported by /etc/nixos/configuration.nix on gitano.
# Source of truth: ~/projects/floodroutes/deploy/gitano/floodroutes.nix (copied into /etc/nixos).
#
# gitano serves only built artifacts; the data pipeline (Postgres, 311 refresh,
# classification, geocoding, graph import) stays on quad. quad pushes updates with
# deploy/gitano/deploy.sh into /srv/floodroutes, which has the repo's layout:
#   api/ web/ risk/*.geojson,png,json data/osm/bayous.geojson
#   graphhopper/{graphhopper-web-11.0.jar, config.yml, custom_models/, graph-cache/}
#
# https://floodroutes.nbolt.com  (CNAME gitano; nginx + Let's Encrypt + basic auth)
# Basic-auth users: /var/lib/floodroutes/htpasswd (not in the Nix store; written by deploy.sh).
{ config, lib, pkgs, ... }:

let
  root = "/srv/floodroutes";
  apiPython = pkgs.python3.withPackages (ps: with ps; [ fastapi uvicorn httpx shapely pyproj ]);
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
      # 4 worker processes: one Python process only uses one core (~20 routes/s on gitano)
      ExecStart = "${apiPython}/bin/uvicorn api.app:app --host 127.0.0.1 --port 8086 --proxy-headers --workers 4";
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
    virtualHosts."floodroutes.nbolt.com" = {
      enableACME = true;
      forceSSL = true;
      basicAuthFile = "/var/lib/floodroutes/htpasswd";
      locations."/" = {
        proxyPass = "http://127.0.0.1:8086";
      };
    };
  };

  networking.firewall.allowedTCPPorts = [ 80 443 ];
}
