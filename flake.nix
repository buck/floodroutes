{
  description = "Houston flood-aware routing (311 history -> GraphHopper)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
    in {
      # Build/batch tools. Python libraries come from the uv venv (pyproject.toml);
      # the systemd services will get a python3.withPackages via nixosModules later.
      devShells.${system}.default = pkgs.mkShell {
        packages = with pkgs; [
          jdk21            # GraphHopper 10+/11 needs Java 17+
          python312
          uv
          postgresql_17    # psql/pg_dump client; server runs in docker (port 5442)
          osmium-tool
          osm2pgsql
          gdal
          curl
          jq
        ];
        shellHook = ''
          export PGHOST=127.0.0.1 PGPORT=5442 PGDATABASE=houston311 PGUSER=postgres
          [ -d .venv ] || uv venv -q --managed-python -p 3.12 .venv  # not a nix-store python: GC would break the venv
          . .venv/bin/activate
          export PGPASSWORD=$(sed -n "s/^POSTGRES_PASSWORD=//p" .env 2>/dev/null)
        '';
      };
    };
}
