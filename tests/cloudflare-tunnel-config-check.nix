# Fast, build-light verification of the Cloudflare Tunnel (remotely-managed)
# wiring.
#
# The tunnel is dashboard-managed and the LXC just runs
# `cloudflared tunnel run --token`. This check evals the module and asserts:
#
#   * the unit runs cloudflared with --token,
#   * the token comes from the agenix secret via systemd LoadCredential into
#     $CREDENTIALS_DIRECTORY (never the literal token in the unit or store),
#   * the unit is hardened (DynamicUser, NoNewPrivileges, ProtectSystem),
#   * a wired token produces no warnings; a missing token warns.
#
# Run with:  nix build .#checks.x86_64-linux.cloudflare-tunnel-config-check

{ nixpkgs }:
let
  system = "x86_64-linux";
  inherit (nixpkgs) lib;
  pkgs = nixpkgs.legacyPackages.${system};

  mkCfg =
    tokenFile:
    (lib.nixosSystem {
      inherit system;
      modules = [
        ../modules/cloudflare-tunnel.nix
        {
          services.cloudflare-tunnel = {
            enable = true;
            inherit tokenFile;
          };
        }
      ];
    }).config;

  ok = mkCfg "/dummy/token";
  missing = mkCfg null;

  # The ExecStart is the wrapper script store path; read its text.
  execScript = builtins.readFile ok.systemd.services."cloudflared-tunnel".serviceConfig.ExecStart;

  unitFile =
    pkgs.writeText "cloudflared-tunnel.unit"
      ok.systemd.units."cloudflared-tunnel.service".text;
  execFile = pkgs.writeText "cloudflared-tunnel.exec" execScript;
  okWarnings = builtins.concatStringsSep "|" ok.warnings;
  missingWarnings = builtins.concatStringsSep "|" missing.warnings;

  need = needle: file: ''
    if ! grep -q -- ${lib.escapeShellArg needle} ${file}; then
      echo "MISSING: ${needle} in ${file}" >&2
      exit 1
    fi
  '';
in
pkgs.runCommand "cloudflare-tunnel-config-check"
  {
    inherit
      unitFile
      execFile
      okWarnings
      missingWarnings
      ;
  }
  ''
    cat $unitFile > unit
    cat $execFile > exec

    # ── runs cloudflared tunnel run --token ────────────────────────────
    ${need "cloudflared tunnel" "exec"}
    ${need "--token" "exec"}
    ${need "CREDENTIALS_DIRECTORY/token" "exec"}

    # ── token from the agenix secret via LoadCredential, never inline ──
    ${need "LoadCredential=token:/dummy/token" "unit"}
    if grep -q "cfut_" unit exec; then
      echo "UNEXPECTED: literal token leaked into unit/wrapper" >&2
      exit 1
    fi

    # ── hardened unit ──────────────────────────────────────────────────
    ${need "DynamicUser=true" "unit"}
    ${need "NoNewPrivileges" "unit"}
    ${need "ProtectSystem=strict" "unit"}
    ${need "WantedBy=multi-user.target" "unit"}

    # ── warnings: wired token is clean, missing token warns ────────────
    test -z "$okWarnings" || { echo "UNEXPECTED: warnings with token wired ($okWarnings)" >&2; exit 1; }
    echo "$missingWarnings" | grep -qi "no tokenFile" \
      || { echo "MISSING: no-tokenFile warning" >&2; exit 1; }

    touch "$out"
    echo "cloudflare-tunnel-config-check passed"
  ''
