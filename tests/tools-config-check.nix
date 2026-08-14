# Fast, build-light verification of the content-store fast-path tooling
# (modules/hermes-tools.nix).
#
# Asserts on the generated artifacts at eval time:
#   * the content-store activation script initialises a git repo, tags it
#     content-good, and symlinks $HERMES_HOME/skills -> content/skills,
#   * hermes-tool is installed for the agent AND the operator,
#   * the gateway PATH appends content/bin (agent tools, no restart needed),
#   * the deploy watchdog carries the content-recovery hook BEFORE any
#     generation rollback (safe, cheap recovery first),
#   * the auto-commit timer exists so direct edits are captured in git.
#
# Run with:  nix build .#checks.x86_64-linux.tools-config-check

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
        (import ./../modules/hermes-tools.nix)
        {
          services.hermes-tools.enable = true;
        }
      ];
    }).config;

  # Generated artifacts to assert on.
  activation =
    pkgs.writeText "hermes-content-activation"
      (cfg.system.activationScripts."hermes-content-store".text or "");
  deployScript = cfg.systemd.services."hermes-deploy".serviceConfig.ExecStart;
  watchdogScript = cfg.systemd.services."hermes-watchdog".serviceConfig.ExecStart;
  agentUnit = pkgs.writeText "hermes-agent.unit" cfg.systemd.units."hermes-agent.service".text;
  contentTimer =
    pkgs.writeText "hermes-content-commit.timer"
      (cfg.systemd.units."hermes-content-commit.timer".text or "");

  findBin = name: lib.findFirst (p: p.name == name) null cfg.environment.systemPackages;
  toolBin = findBin "hermes-tool";

  # hermes-agent.extraPackages is a list of packages; the agent-facing hermes-tool
  # shows up in the hermes user's profile packages.
  agentToolPkgs = cfg.users.users.hermes.packages or [];
  agentHasTool = lib.any (p: (p.name or "") == "hermes-tool") agentToolPkgs;

  need = needle: file: ''
    if ! grep -q -- ${lib.escapeShellArg needle} ${file}; then
      echo "MISSING: ${needle} in ${file}" >&2
      exit 1
    fi
  '';
in
pkgs.runCommand "tools-config-check"
  {
    inherit activation deployScript watchdogScript agentUnit contentTimer;
    toolBinPath = if toolBin == null then "/nonexistent" else "${toolBin}/bin/hermes-tool";
    agentHasTool = lib.boolToString agentHasTool;
    contentRecovery = cfg.services.hermes-deploy.contentRecovery;
  }
  ''
    cat $activation > activation.script
    cat $deployScript > deploy.script
    cat $watchdogScript > watchdog.script
    cat $agentUnit > agent.unit
    cat $contentTimer > content.timer

    # ── activation: git repo + content-good tag + skills symlink ────────
    ${need "init -q" "activation.script"}
    ${need "content-good" "activation.script"}
    ${need "ln -sfn" "activation.script"}
    ${need "skills" "activation.script"}
    ${need "hermes-content" "activation.script"}

    # ── hermes-tool installed for agent (extraPackages -> user profile) ──
    test "$agentHasTool" = "true" || { echo "MISSING: hermes-tool not in agent extraPackages" >&2; exit 1; }

    # ── gateway PATH appends content/bin (lowest precedence) ────────────
    ${need "content/bin" "agent.unit"}

    # ── deploy + watchdog carry content recovery before generation rollback
    ${need "content recovery first" "deploy.script"}
    ${need "content recovery restored health" "deploy.script"}
    ${need "content recovery first" "watchdog.script"}
    ${need "content recovery restored health" "watchdog.script"}
    ${need "rolling back exactly one generation" "watchdog.script"}

    # ── contentRecovery wired to hermes-tool revert (the recovery floor) ──
    echo "$contentRecovery" | grep -q "hermes-tool" \
      || { echo "MISSING: contentRecovery not set to hermes-tool" >&2; exit 1; }
    echo "$contentRecovery" | grep -q "revert" \
      || { echo "MISSING: contentRecovery not a revert" >&2; exit 1; }

    # ── auto-commit timer exists so direct edits are captured in git ─────
    ${need "OnUnitActiveSec" "content.timer"}

    # ── hermes-tool binary is a store path (immutable, recovery floor) ───
    case "$toolBinPath" in
      /nix/store/*) ;;
      *) echo "FAIL: hermes-tool not a store path: $toolBinPath" >&2; exit 1 ;;
    esac

    touch "$out"
    echo "tools-config-check passed"
  ''
