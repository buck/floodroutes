# floodroutes: imported by /etc/nixos/configuration.nix on quad.
# Source of truth: ~/projects/floodroutes/deploy/quad/floodroutes.nix (copied into /etc/nixos).
#
# Database: Docker container floodroutes-db (postgis 18-3.6) on 127.0.0.1:5442,
# data in /new/floodroutes/pgdata, started from the repo's docker-compose.yml
# with restart: unless-stopped.
# GraphHopper 11 on 127.0.0.1:8989 (admin 8990); API + map page on 127.0.0.1:8086.
{ config, lib, pkgs, ... }:

let
  floodroutesPython = pkgs.python3.withPackages (ps: with ps; [
    psycopg pyyaml h3 shapely pyproj httpx fastapi uvicorn matplotlib
  ]);
  java = "${pkgs.jdk21_headless}/bin/java";
  repo = "/home/buck/projects/floodroutes";
in
{
  systemd.tmpfiles.rules = [ "d /new/floodroutes 0755 buck users -" ];

  # Re-download the MTD and current-year YTD 311 extracts, upsert, reclassify,
  # geocode new flood signals. The city regenerates both around 21:00-21:15 local.
  systemd.services.floodroutes-refresh = {
    description = "floodroutes: refresh Houston 311 MTD/YTD extracts";
    after = [ "network-online.target" "docker.service" ];
    wants = [ "network-online.target" ];
    path = with pkgs; [ bash coreutils curl gnused ];
    serviceConfig = {
      Type = "oneshot";
      User = "buck";
      WorkingDirectory = repo;
      Environment = [ "PYTHON=${floodroutesPython}/bin/python" ];
      ExecStart = "${repo}/refresh.sh";
      TimeoutStartSec = "2h";
    };
  };
  systemd.timers.floodroutes-refresh = {
    wantedBy = [ "timers.target" ];
    timerConfig = { OnCalendar = "*-*-* 23:30:00"; Persistent = true; RandomizedDelaySec = "10m"; };
  };

  # Routing server. graphhopper/rebuild.sh stops it, reimports, and starts it again.
  systemd.services.floodroutes-graphhopper = {
    description = "floodroutes: GraphHopper (car, car_flood) on :8989";
    after = [ "network.target" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "simple";
      User = "buck";
      WorkingDirectory = "${repo}/graphhopper";
      ExecStart = "${java} -Xmx4g -jar graphhopper-web-11.0.jar server config.yml";
      Restart = "on-failure";
      RestartSec = "10s";
    };
  };

  systemd.services.floodroutes-api = {
    description = "floodroutes: route comparison API and map page on :8086";
    after = [ "floodroutes-graphhopper.service" "network.target" ];
    wants = [ "floodroutes-graphhopper.service" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "simple";
      User = "buck";
      WorkingDirectory = repo;
      ExecStart = "${floodroutesPython}/bin/uvicorn api.app:app --host 127.0.0.1 --port 8086";
      Restart = "on-failure";
      RestartSec = "5s";
    };
  };
}
