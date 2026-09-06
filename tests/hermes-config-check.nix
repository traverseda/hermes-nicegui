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

  # Activation snippet that bootstraps /var/lib/hermes-deploy as a git repo
  # on the box (the deploy/rollback git phases silently no-op without it).
  repoActivation = pkgs.writeText "hermes-deploy-repo.activation" (
    cfg.system.activationScripts."hermes-deploy-repo".text or ""
  );

  gitconfig = pkgs.writeText "gitconfig" cfg.environment.etc."gitconfig".text;

  # CLI wrappers are writeShellScriptBin derivations; find them by name.
  findBin = name: lib.findFirst (p: p.name == name) null cfg.environment.systemPackages;
  statusBin = findBin "hermes-status";
  deployBin = findBin "hermes-deploy";
  rollbackBin = findBin "hermes-rollback";

  # git/openssh must be on the box (not just in the agent's sandbox): the
  # deploy/rollback/watchdog scripts and `hermes-status` all run git against
  # the flake repo. A box without git cannot roll back. (Regression guard for
  # the `git: command not found` failure the integration test used to mask.)
  hasPackage =
    prefix: lib.any (p: lib.hasPrefix prefix (p.name or "")) cfg.environment.systemPackages;
  gitPresent = if hasPackage "git-" then "yes" else "no";
  sshPresent = if hasPackage "openssh-" then "yes" else "no";

  need = needle: file: ''
    if ! grep -q -- ${lib.escapeShellArg needle} ${file}; then
      echo "MISSING: ${needle} in ${file}" >&2
      exit 1
    fi
  '';

  mustNot = needle: file: ''
    if grep -q -- ${lib.escapeShellArg needle} ${file}; then
      echo "UNEXPECTED: ${needle} in ${file}" >&2
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
    inherit gitPresent sshPresent repoActivation gitconfig;
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
    cat $repoActivation > hermes-deploy-repo.activation
    cat $gitconfig > gitconfig

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
    ${need "nix-env -p /nix/var/nix/profiles/system --rollback" "rollback.script"}
    ${need "switch-to-configuration switch" "rollback.script"}
    ${need "last-known-good" "watchdog.script"}
    ${need "rolling back exactly one generation" "watchdog.script"}
    ${need "not auto-rolling back" "watchdog.script"}

    # The deploy-time auto-rollback must use the same low-level rollback, NOT
    # `nixos-rebuild switch <store-path>` (prints usage, does nothing) and NOT
    # `nixos-rebuild switch --rollback` (self-locates via NIX_PATH, which a
    # flake-only box lacks).
    ${need "nix-env -p /nix/var/nix/profiles/system --rollback" "deploy.script"}
    if grep -q 'nixos-rebuild switch .*readlink' deploy.script; then
      echo "FAIL: deploy.script passes a store path to nixos-rebuild switch (broken rollback)" >&2
      exit 1
    fi
    if grep -q 'nixos-rebuild switch --rollback' deploy.script rollback.script watchdog.script; then
      echo "FAIL: scripts use nixos-rebuild --rollback (needs NIX_PATH; fails on a flake-only box)" >&2
      exit 1
    fi

    # currentGeneration must come from the ACTIVE profile link, not
    # `list-generations | tail -n1` (stale high number after --rollback →
    # watchdog walks past last-known-good, one generation per run, forever).
    ${need "readlink /nix/var/nix/profiles/system" "deploy.script"}
    ${need "readlink /nix/var/nix/profiles/system" "rollback.script"}
    ${need "readlink /nix/var/nix/profiles/system" "watchdog.script"}
    if grep -q 'list-generations.*tail -n1' deploy.script; then
      echo "FAIL: currentGeneration must not be derived from tail -n1" >&2
      exit 1
    fi

    # currentGeneration's sed must ACTUALLY extract the number from a
    # `system-<N>-link` symlink. A pattern expecting `-system-` never matches
    # (silently empty), which breaks the watchdog's rollback guard and writes
    # an empty last-known-good. This shipped broken once — test the output.
    if ! echo "system-10-link" | sed -n 's/.*system-\([0-9][0-9]*\)-link$/\1/p' | grep -qx '10'; then
      echo "FAIL: currentGeneration sed pattern does not match system-10-link" >&2
      exit 1
    fi
    ${need "sed -n 's/.*system-" "deploy.script"}

    # ── git-repo bootstrap activation snippet ──────────────────────────
    # Without this, /var/lib/hermes-deploy on the box is NOT a git repo and
    # every deploy/rollback git phase silently no-ops (`|| true`).
    ${need "init -q" "hermes-deploy-repo.activation"}
    ${need "chown" "hermes-deploy-repo.activation"}
    # vendor/* submodules (hermes-agent/hermes-nicegui/xaelWiki source) are
    # real source code in the system lane now — they must be TRACKED by the
    # ledger (not excluded), or a generation rollback can never actually
    # restore a bad self-edit to vendor/hermes-agent. Regression guard for
    # the previous design, which explicitly excluded vendor/ from the ledger.
    ${mustNot "vendor" "hermes-deploy-repo.activation"}

    # ── vendor/* submodules are materialized on first sync, never force-
    #    reset (a code change ships by committing INSIDE the submodule, not
    #    by the outer repo clobbering it) ───────────────────────────────
    ${need "submodule update --init" "deploy.script"}

    # ── generation <-> git-commit tagging + submodule-aware rollback ────
    # Every switch tags the commit that produced it (`gen-<N>`); any
    # rollback path resets the outer repo to that tag AND runs `git
    # submodule update`, so vendor/* checkouts move in lockstep with the
    # Nix generation — without this, rolling back a generation would not
    # actually undo a bad edit to vendor/hermes-agent's own source, since
    # the working tree would stay at the newer, bad commit.
    ${need "tag_gen()" "deploy.script"}
    ${need "sync_repo_to_gen()" "deploy.script"}
    ${need "sync_repo_to_gen()" "rollback.script"}
    ${need "sync_repo_to_gen()" "watchdog.script"}
    ${need "submodule update --init --recursive" "rollback.script"}
    ${need "submodule update --init --recursive" "watchdog.script"}
    ${need ''tag -f "gen-$1" HEAD'' "deploy.script"}

    # ── vendor/* submodules must be root-git-safe too ───────────────────
    # `sync_repo_to_gen`'s `git submodule update` operates INSIDE each
    # vendor/* submodule as root; without a safe.directory entry per
    # submodule, that fails with "detected dubious ownership" and a
    # rollback silently leaves vendor/* at the wrong commit.
    ${need "directory = /var/lib/hermes-deploy/vendor/hermes-agent" "gitconfig"}
    ${need "directory = /var/lib/hermes-deploy/vendor/hermes-nicegui" "gitconfig"}
    ${need "directory = /var/lib/hermes-deploy/vendor/xaelWiki" "gitconfig"}

    # ── agent gateway unit exists and is enabled ───────────────────────
    # The bot is root by design (hosts/hermes/configuration.nix): the unit
    # must NOT carry the old kernel sandbox (NoNewPrivileges / ProtectSystem
    # blocked sudo/setuid entirely — NoNewPrivs:1 in the bot's process made
    # the passwordless-sudo grant useless). Safety is the rollback machinery.
    ${need "NoNewPrivileges=false" "agent.unit"}
    ${need "ProtectSystem=false" "agent.unit"}
    # sudo (setuid wrapper) and system tools must be on the bot's service PATH.
    ${need "/run/wrappers" "agent.unit"}
    test "$agentEnabled" = "true"

    # ── CLI wrappers ───────────────────────────────────────────────────
    ${need "generations" "status.bin"}
    ${need "systemctl start hermes-deploy.service" "deploy.bin"}
    ${need "systemctl start hermes-rollback.service" "rollback.bin"}

    # ── git/openssh are on the box (deploy machinery is git-driven) ────
    test "$gitPresent" = "yes" || { echo "MISSING: git not in systemPackages — deploy/rollback cannot run" >&2; exit 1; }
    test "$sshPresent" = "yes" || { echo "MISSING: openssh not in systemPackages — ssh git remotes cannot fetch" >&2; exit 1; }

    # ── tailscale wiring (auth key is wired regardless of the VM-only
    #    `enable = false` override) ─────────────────────────────────────
    test "$tailscaleAuthKeyFile" = "/run/agenix/tailscale-auth"

    touch "$out"
    echo "hermes-config-check passed"
  ''
