# Fast, build-light verification of the hermes-nicegui wiring.
#
# Like the other *-config-check targets, this evals the hermes-nicegui module
# and asserts on the generated systemd unit / environment *text* at eval time
# — seconds, not a VM boot.
#
# Run with:  nix build .#checks.x86_64-linux.nicegui-config-check

{ nixpkgs, hermes-agent }:
let
  system = "x86_64-linux";
  inherit (nixpkgs) lib;
  pkgs = nixpkgs.legacyPackages.${system};

  cfg =
    (lib.nixosSystem {
      inherit system;
      modules = [
        hermes-agent.nixosModules.default
        ../modules/hermes-service.nix
        ../modules/hermes-deploy.nix
        ../modules/hermes-nicegui.nix
        ../modules/exposure.nix
        {
          services.hermes-nicegui = {
            enable = true;
            environmentFile = "/dummy/nicegui-env";
          };
        }
      ];
    }).config;

  niceguiUnit =
    pkgs.writeText "hermes-nicegui.unit"
      cfg.systemd.units."hermes-nicegui.service".text;
  tailscalePorts = builtins.concatStringsSep "," (
    map toString cfg.networking.firewall.interfaces.tailscale0.allowedTCPPorts
  );

  # grep -q with a clear message on failure.
  need = needle: file: ''
    if ! grep -q -- ${lib.escapeShellArg needle} ${file}; then
      echo "MISSING: ${needle} in ${file}" >&2
      exit 1
    fi
  '';

  # grep -q with a clear message when a string is PRESENT but must not be.
  mustNot = needle: file: ''
    if grep -q -- ${lib.escapeShellArg needle} ${file}; then
      echo "UNEXPECTED: ${needle} in ${file}" >&2
      exit 1
    fi
  '';
in
pkgs.runCommand "nicegui-config-check"
  {
    inherit niceguiUnit tailscalePorts;
  }
  ''
    cat $niceguiUnit > nicegui.unit

    # ── unit exists, runs the editable checkout with the Nix python env ──
    ${need "ExecStart=" "nicegui.unit"}
    ${need "/bin/python /var/lib/hermes-nicegui/src/main.py" "nicegui.unit"}
    ${need "PYTHONPATH=/var/lib/hermes-nicegui/src/src:" "nicegui.unit"}
    ${need "hermes-nicegui-dist-info" "nicegui.unit"}
    ${need "WantedBy=multi-user.target" "nicegui.unit"}

    # ── runs as the hermes user, hardened like the other units ──────────
    ${need "User=hermes" "nicegui.unit"}
    ${need "Group=hermes" "nicegui.unit"}
    ${need "NoNewPrivileges" "nicegui.unit"}
    ${need "ProtectSystem=strict" "nicegui.unit"}
    ${need "WorkingDirectory=" "nicegui.unit"}

    # ── prod-safe runtime settings (no reloader, loopback bind) ─────────
    ${need "HERMES_UI_HOST=127.0.0.1" "nicegui.unit"}
    ${need "HERMES_UI_PORT=8080" "nicegui.unit"}
    ${need "HERMES_UI_RELOAD=false" "nicegui.unit"}
    ${need "HERMES_AUTH_ENABLED=true" "nicegui.unit"}
    ${need "HERMES_EXEC_MODE=local" "nicegui.unit"}
    ${need "HERMES_KANBAN_URL=http://127.0.0.1:9119" "nicegui.unit"}
    ${need "HERMES_DATA_DIR=/var/lib/hermes-nicegui" "nicegui.unit"}

    # ── the `hermes` CLI subprocess uses the agent's own binary ─────────
    ${need "HERMES_CLI_BIN=/nix/store/" "nicegui.unit"}
    ${need "/bin/hermes" "nicegui.unit"}
    ${need "HERMES_HOME=" "nicegui.unit"}

    # ── kanban creds flow in via systemd EnvironmentFile ────────────────
    ${need "EnvironmentFile=/dummy/nicegui-env" "nicegui.unit"}

    # ── NOT opened on the tailnet: public path is the tunnel, loopback only
    if [ -n "$tailscalePorts" ]; then
      echo "UNEXPECTED: tailscale0 allowedTCPPorts=$tailscalePorts (should be empty — hermes-nicegui binds loopback and is reached via the Cloudflare tunnel)" >&2
      exit 1
    fi

    touch "$out"
    echo "nicegui-config-check passed"
  ''
