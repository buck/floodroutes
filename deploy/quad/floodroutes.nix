# floodroutes: imported by /etc/nixos/configuration.nix on quad.
# Source of truth: ~/projects/floodroutes/deploy/quad/floodroutes.nix (copied into /etc/nixos).
#
# Database: Docker container floodroutes-db (postgis 18-3.6) on 127.0.0.1:5442,
# data in /new/floodroutes/pgdata, started from the repo's docker-compose.yml
# with restart: unless-stopped.
{ config, lib, pkgs, ... }:

let
  floodroutesPython = pkgs.python3.withPackages (ps: with ps; [ psycopg ]);
  repo = "/home/buck/projects/floodroutes";
in
{
  systemd.tmpfiles.rules = [ "d /new/floodroutes 0755 buck users -" ];

  # Re-download the MTD and current-year YTD 311 extracts and upsert them.
  # The city regenerates both around 21:00-21:15 local.
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
}
