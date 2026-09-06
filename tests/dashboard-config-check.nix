# Fast, build-light verification of the Hermes dashboard wiring.
#
# Like the other *-config-check targets, this evals the dashboard module
# and asserts on the generated systemd unit / firewall *text* at eval time
# — seconds, not a VM boot.
#
# Run with:  nix build .#checks.x86_64-linux.dashboard-config-check

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
        ../modules/hermes-dashboard.nix
        ../modules/exposure.nix
        ../modules/config.nix
        {
          hermesDeploy.providers.test = {
            name    = "test";
            provider= "vllm";
            model   = "test-model";
          };
          hermesDeploy.defaultProvider = "test";

          services.hermes-dashboard = {
            enable = true;
            environmentFile = "/dummy/dashboard-env";
          };
        }
      ];
    }).config;

  dashboardUnit =
    pkgs.writeText "hermes-dashboard.unit"
      cfg.systemd.units."hermes-dashboard.service".text;
  agentEnvFiles = builtins.concatStringsSep "," cfg.services.hermes-agent.environmentFiles;
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
in
pkgs.runCommand "dashboard-config-check"
  {
    inherit dashboardUnit agentEnvFiles tailscalePorts;
  }
  ''
    cat $dashboardUnit > dashboard.unit

    # ── dashboard unit exists, runs the web UI, hardened like the gateway ─
    ${need "hermes dashboard" "dashboard.unit"}
    ${need "--host 0.0.0.0" "dashboard.unit"}
    ${need "--port 9119" "dashboard.unit"}
    ${need "--no-open" "dashboard.unit"}
    ${need "After=hermes-agent.service" "dashboard.unit"}
    ${need "Wants=hermes-agent.service" "dashboard.unit"}
    ${need "WantedBy=multi-user.target" "dashboard.unit"}
    ${need "NoNewPrivileges" "dashboard.unit"}
    # Bot is root: ProtectSystem relaxed (module uses mkForce false) so
    # the bot's root access actually works; safety net is rollback, not
    # kernel sandbox.
    ${need "ProtectSystem=false" "dashboard.unit"}
    ${need "User=hermes" "dashboard.unit"}
    ${need "Group=hermes" "dashboard.unit"}

    # ── credentials flow into the default profile .env (hermes-agent) ────
    if ! echo "$agentEnvFiles" | grep -q "/dummy/dashboard-env"; then
      echo "MISSING: dashboard environmentFile in hermes-agent.environmentFiles ($agentEnvFiles)" >&2
      exit 1
    fi

    # ── tailnet-only exposure ─────────────────────────────────────────────
    if [ "$tailscalePorts" != "9119" ]; then
      echo "UNEXPECTED: tailscale0 allowedTCPPorts=$tailscalePorts" >&2
      exit 1
    fi

    touch "$out"
    echo "dashboard-config-check passed"
  ''
