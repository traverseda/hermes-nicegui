# Fast, build-light verification of the Hermes deploy/rollback/watchdog wiring.
#
# Asserts on the generated systemd units, scripts and CLI wrappers *text* at
# eval time (seconds), instead of booting a VM. It does NOT replace the VM
# integration test for runtime behaviour (the gateway actually starting, and
# the rollback script running against an empty generation history).
#
# Run with:  nix build .#checks.x86_64-linux.hermes-config-check

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
        (import ./vm-configuration.nix)
        {
          # Mirror the VM integration test's long watchdog interval.
          services.hermes-deploy.watchdogInterval = "1h";
        }
      ];
    }).config;

  # Generated artifacts to assert on (store paths / small strings).
  deployScript = cfg.systemd.services."hermes-deploy".serviceConfig.ExecStart;
  rollbackScript = cfg.systemd.services."hermes-rollback".serviceConfig.ExecStart;
  watchdogScript = cfg.systemd.services."hermes-watchdog".serviceConfig.ExecStart;
  deployUnit = pkgs.writeText "hermes-deploy.unit" cfg.systemd.units."hermes-deploy.service".text;
  rollbackUnit =
    pkgs.writeText "hermes-rollback.unit"
      cfg.systemd.units."hermes-rollback.service".text;
  watchdogUnit =
    pkgs.writeText "hermes-watchdog.unit"
      cfg.systemd.units."hermes-watchdog.service".text;
  watchdogTimer =
    pkgs.writeText "hermes-watchdog.timer"
      cfg.systemd.units."hermes-watchdog.timer".text;
  agentUnit = pkgs.writeText "hermes-agent.unit" cfg.systemd.units."hermes-agent.service".text;

  # CLI wrappers are writeShellScriptBin derivations; find them by name.
  findBin = name: lib.findFirst (p: p.name == name) null cfg.environment.systemPackages;
  statusBin = findBin "hermes-status";
  deployBin = findBin "hermes-deploy";
  rollbackBin = findBin "hermes-rollback";

  need = needle: file: ''
    if ! grep -q -- ${lib.escapeShellArg needle} ${file}; then
      echo "MISSING: ${needle} in ${file}" >&2
      exit 1
    fi
  '';
in
pkgs.runCommand "hermes-config-check"
  {
    inherit deployScript rollbackScript watchdogScript;
    inherit
      deployUnit
      rollbackUnit
      watchdogUnit
      watchdogTimer
      agentUnit
      ;
    inherit statusBin deployBin rollbackBin;
    tailscaleAuthKeyFile = cfg.services.tailscale.authKeyFile;
    agentEnabled = lib.boolToString cfg.services.hermes-agent.enable;
  }
  ''
    cat $deployUnit > deploy.unit
    cat $rollbackUnit > rollback.unit
    cat $watchdogUnit > watchdog.unit
    cat $watchdogTimer > watchdog.timer
    cat $agentUnit > agent.unit
    cat $deployScript > deploy.script
    cat $rollbackScript > rollback.script
    cat $watchdogScript > watchdog.script
    cat $statusBin/bin/hermes-status > status.bin
    cat $deployBin/bin/hermes-deploy > deploy.bin
    cat $rollbackBin/bin/hermes-rollback > rollback.bin

    # ── deploy/rollback/watchdog units exist and are wired ─────────────
    ${need "ExecStart" "deploy.unit"}
    ${need "ExecStart" "rollback.unit"}
    ${need "ExecStart" "watchdog.unit"}
    ${need "After=hermes-agent.service" "watchdog.unit"}
    ${need "Requires=hermes-agent.service" "watchdog.unit"}
    ${need "OnBootSec=10min" "watchdog.timer"}
    ${need "OnUnitActiveSec=1h" "watchdog.timer"}   # vm-configuration overrides

    # ── scripts carry the rollback-safety logic ────────────────────────
    ${need "nixos-rebuild switch" "deploy.script"}
    ${need "AGENT REPORTED UNHEALTHY" "deploy.script"}
    ${need "last-known-good" "deploy.script"}
    ${need "nixos-rebuild switch --rollback" "rollback.script"}
    ${need "last-known-good" "watchdog.script"}
    ${need "rolling back exactly one generation" "watchdog.script"}
    ${need "not auto-rolling back" "watchdog.script"}

    # ── submodules (vendor/xaelWiki) are materialized, never force-reset ─
    # `submodule update --init` (no --force) means the editable xaelwiki
    # source survives deploys; a fresh checkout still gets materialized.
    ${need "submodule update --init" "deploy.script"}
    ${need "submodule update --init" "rollback.script"}

    # ── agent gateway unit exists (hardened) and is enabled ────────────
    ${need "NoNewPrivileges" "agent.unit"}
    ${need "ProtectSystem=strict" "agent.unit"}
    test "$agentEnabled" = "true"

    # ── CLI wrappers ───────────────────────────────────────────────────
    ${need "generations" "status.bin"}
    ${need "systemctl start hermes-deploy.service" "deploy.bin"}
    ${need "systemctl start hermes-rollback.service" "rollback.bin"}

    # ── tailscale wiring (auth key is wired regardless of the VM-only
    #    `enable = false` override) ─────────────────────────────────────
    test "$tailscaleAuthKeyFile" = "/run/agenix/tailscale-auth"

    touch "$out"
    echo "hermes-config-check passed"
  ''
