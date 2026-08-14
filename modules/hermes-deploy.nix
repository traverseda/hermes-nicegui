# Git-driven deployment + health-checked auto-rollback for Hermes.
#
# This is the heart of the "bot changes itself safely" story.
#
# How a change ships:
#   1. The bot (or a human) commits to the flake repo.
#   2. `hermes-deploy` pulls the repo, runs `nixos-rebuild switch`, and
#      health-checks the gateway — by asking the agent itself (hermes doctor).
#      A bad deploy is rolled back automatically (one generation).
#   3. A watchdog timer keeps checking; if the agent reports unhealthy and the
#      current generation is newer than the last-known-good generation, it
#      rolls back — at most ONE generation per check, so it can never jump
#      past generations and converges on the last good state one step at a
#      time.
#   4. `hermes-rollback` does a git revert + roll back one generation.
#
# Rollback layers (defence in depth):
#   * Git history of the flake repo — revert any self-edit.
#   * Nix generations — rolling back one generation returns to the previous
#     system state; the store keeps every referenced generation.
#   * Last-known-good generation — the watchdog only ever rolls back *past*
#     the newest generation that was observed healthy, so a deliberately
#     stopped service (or a planned outage) is never auto-rolled-back.
#   * Proxmox snapshots (`pct snapshot`) — external safety net, scripted in
#     scripts/deploy.sh.

{
  config,
  pkgs,
  lib,
  ...
}:

let
  cfg = config.services.hermes-deploy;

  currentGeneration = ''
    nix-env -p /nix/var/nix/profiles/system --list-generations \
      | tail -n1 | awk '{print $1}'
  '';

  healthCheckScript = pkgs.writeShellScript "hermes-health-check" cfg.healthCheck;

  # Content-lane recovery hook: when the agent is unhealthy, try reverting the
  # fast-path content store (skills/tools/MCP registrations) BEFORE spending a
  # Nix generation. Set by modules/hermes-tools.nix; empty = no content lane.
  contentRecoveryScript = lib.optionalString (cfg.contentRecovery != "")
    (pkgs.writeShellScript "hermes-content-recovery" cfg.contentRecovery);

  deployScript = pkgs.writeShellScript "hermes-deploy-script" ''
    set -euo pipefail

    log() { echo "[hermes-deploy] $*"; }

    # No git remote: the checkout in ${cfg.repoDir} IS the source of truth.
    # The operator pushes a fresh copy (scripts/deploy.sh rsyncs it), and the
    # bot commits directly here then rebuilds. Record any uncommitted edits in
    # the local ledger so the deploy is always a committed state (rollback
    # = git reset HEAD~1).
    git -C ${cfg.repoDir} config user.name "hermes-deploy"
    git -C ${cfg.repoDir} config user.email "hermes-deploy@localhost"
    git -C ${cfg.repoDir} add -A
    git -C ${cfg.repoDir} commit -q -m "deploy $(date -Is)" 2>/dev/null || true
    # Materialize git submodules (e.g. vendor/xaelWiki) on first sync. Never
    # --force: local edits inside a submodule (xaelwiki source) must survive
    # deploys — that is the "editable, low-stakes" design.
    git -C ${cfg.repoDir} submodule update --init 2>/dev/null || true

    PREV_GEN=$(${currentGeneration})
    log "previous generation: $PREV_GEN"

    log "building & switching generation"
    nixos-rebuild switch --flake "${cfg.repoDir}#${cfg.flakeAttr}" 2>&1 \
      | tee -a /var/log/hermes-deploy.log

    CUR_GEN=$(${currentGeneration})

    log "asking agent for a health report (grace ${toString cfg.gracePeriod}s)"
    if timeout ${toString cfg.gracePeriod} bash ${healthCheckScript}; then
      echo "deploy OK (generation $CUR_GEN)"
      echo "$CUR_GEN" > ${cfg.stateDir}/last-known-good
      exit 0
    fi

    ${lib.optionalString (cfg.contentRecovery != "") ''
      log "AGENT REPORTED UNHEALTHY — trying content recovery first"
      if timeout ${toString cfg.gracePeriod} bash ${contentRecoveryScript}; then
        sleep 10
        if timeout ${toString cfg.gracePeriod} bash ${healthCheckScript}; then
          echo "content recovery restored health (generation $CUR_GEN)"
          echo "$CUR_GEN" > ${cfg.stateDir}/last-known-good
          exit 0
        fi
      fi
    ''}

    log "AGENT REPORTED UNHEALTHY — rolling back one generation to $PREV_GEN"
    nixos-rebuild switch "$(readlink -f /nix/var/nix/profiles/system-''${PREV_GEN}-link)" 2>&1 \
      | tee -a /var/log/hermes-deploy.log || true
    echo "rolled back from generation $CUR_GEN to $PREV_GEN"
    exit 1
  '';

  rollbackScript = pkgs.writeShellScript "hermes-rollback-script" ''
    set -euo pipefail
    log() { echo "[hermes-rollback] $*"; }

    # No git remote: the checkout in ${cfg.repoDir} IS the source of truth.
    # Step the local ledger back one commit (the deploy script guarantees the
    # active state is always a commit), then roll back one generation.
    log "reverting flake repo by one commit"
    git -C ${cfg.repoDir} reset --hard HEAD~1 2>/dev/null || true
    git -C ${cfg.repoDir} submodule update --init 2>/dev/null || true

    log "rolling back system by one generation"
    nixos-rebuild switch --rollback 2>&1 | tee -a /var/log/hermes-deploy.log

    # Mark this generation known-good only if the agent reports healthy again.
    sleep 10
    if timeout ${toString cfg.gracePeriod} bash ${healthCheckScript}; then
      echo "$(${currentGeneration})" > ${cfg.stateDir}/last-known-good
    fi
  '';

  watchdogScript = pkgs.writeShellScript "hermes-watchdog-script" ''
    set -euo pipefail
    log() { echo "[hermes-watchdog] $*"; }

    KNOWN_GOOD="${cfg.stateDir}/last-known-good"
    mkdir -p ${cfg.stateDir}

    if timeout ${toString cfg.gracePeriod} bash ${healthCheckScript}; then
      CUR_GEN=$(${currentGeneration})
      if [ ! -f "$KNOWN_GOOD" ] || [ "$(cat "$KNOWN_GOOD")" != "$CUR_GEN" ]; then
        echo "$CUR_GEN" > "$KNOWN_GOOD"
        log "healthy — updated last-known-good to generation $CUR_GEN"
      fi
      exit 0
    fi

    log "unhealthy: agent failed its own health report"

    # Content-lane first: try reverting skills/tools/MCP registrations before
    # spending a generation. Cheap, no rebuild; content is the likely culprit
    # for a fast-path change that broke something non-systemic.
    ${lib.optionalString (cfg.contentRecovery != "") ''
      log "unhealthy — trying content recovery first"
      if timeout ${toString cfg.gracePeriod} bash ${contentRecoveryScript}; then
        sleep 10
        if timeout ${toString cfg.gracePeriod} bash ${healthCheckScript}; then
          CUR_GEN=$(${currentGeneration})
          echo "$CUR_GEN" > "$KNOWN_GOOD"
          log "content recovery restored health — updated last-known-good to generation $CUR_GEN"
          exit 0
        fi
      fi
    ''}

    if [ ! -f "$KNOWN_GOOD" ]; then
      log "no last-known-good recorded; not auto-rolling back"
      exit 0
    fi

    CUR_GEN=$(${currentGeneration})
    LAST_GOOD=$(cat "$KNOWN_GOOD")
    if [ "$CUR_GEN" -gt "$LAST_GOOD" ] 2>/dev/null; then
      log "current generation $CUR_GEN is newer than last-known-good $LAST_GOOD"
      log "rolling back exactly one generation"
      nixos-rebuild switch --rollback 2>&1 | tee -a /var/log/hermes-deploy.log || true
      exit 1
    fi

    log "generation $CUR_GEN <= last-known-good $LAST_GOOD; unhealthy but not a new deploy — leaving as-is"
    exit 0
  '';
in
{
  options.services.hermes-deploy = {
    enable = lib.mkEnableOption "hermes git-driven deploy + auto-rollback";

    repoDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/hermes-deploy";
      description = "Git checkout of this flake on the target host.";
    };

    flakeAttr = lib.mkOption {
      type = lib.types.str;
      default = "hermes";
      description = "nixosConfigurations attribute to deploy.";
    };

    branch = lib.mkOption {
      type = lib.types.str;
      default = "main";
      description = "Git branch the deploy script tracks.";
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/hermes-deploy/state";
      description = "State directory for deploy bookkeeping (last-known-good).";
    };

    gracePeriod = lib.mkOption {
      type = lib.types.int;
      default = 120;
      description = "Seconds to wait for the agent's health report after a switch.";
    };

    contentRecovery = lib.mkOption {
      type = lib.types.str;
      default = "";
      description = ''
        Command run when the agent is unhealthy, BEFORE falling back to a
        generation rollback (in both the deploy script and the watchdog). Set
        by the content-store module (services.hermes-tools) to `hermes-tool
        revert`, which restores the last-known-good skills/tools/config from
        git at content granularity — no Nix rebuild. Empty = no content lane.
      '';
    };

    healthCheck = lib.mkOption {
      type = lib.types.str;
      default = ''
        # Ask the agent itself: the gateway unit must be active AND `hermes
        # doctor` (the agent's own self-diagnostic) must pass. Derived from the
        # agent's own options so it stays coherent if stateDir/user change.
        systemctl is-active --quiet hermes-agent \
          && runuser -u ${config.services.hermes-agent.user} -- env HERMES_HOME=${config.services.hermes-agent.stateDir}/.hermes \
               hermes doctor >/dev/null 2>&1
      '';
      description = "Bash command; exit 0 = healthy. Runs as root and should ask Hermes itself.";
    };

    watchdogInterval = lib.mkOption {
      type = lib.types.str;
      default = "15min";
      description = "How often the watchdog re-checks gateway health.";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.services.hermes-deploy = {
      description = "Deploy Hermes from git and health-check (with auto-rollback)";
      wantedBy = [ ];
      path = [ config.system.path ];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = deployScript;
        TimeoutStartSec = 0;
      };
    };

    systemd.services.hermes-rollback = {
      description = "Roll back Hermes to the previous git/system generation";
      wantedBy = [ ];
      path = [ config.system.path ];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = rollbackScript;
        TimeoutStartSec = 0;
      };
    };

    systemd.services.hermes-watchdog = {
      description = "Auto-rollback watchdog for the Hermes gateway";
      after = [ "hermes-agent.service" ];
      requires = [ "hermes-agent.service" ];
      path = [ config.system.path ];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = watchdogScript;
        TimeoutStartSec = 0;
      };
    };

    systemd.timers.hermes-watchdog = {
      description = "Periodic Hermes health check";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "10min";
        OnUnitActiveSec = cfg.watchdogInterval;
        Persistent = true;
      };
    };

    # Keep enough generations that rollback always has somewhere to go.
    nix.gc = {
      automatic = true;
      dates = "daily";
      options = "--delete-older-than 30d";
    };

    # git is a hard dependency of the deploy/rollback/watchdog scripts and of
    # `hermes-status` — they all run git against the flake repo. It must be on
    # the BOX (via system.path, which the units above inherit), not just in the
    # agent's sandbox. openssh covers git-over-ssh remotes (deploy keys).
    environment.systemPackages =
      with pkgs;
      [
        git
        openssh
      ]
      ++ [
        # Convenience CLI wrappers for the operator / the bot.
        (pkgs.writeShellScriptBin "hermes-deploy" ''
          exec systemctl start hermes-deploy.service
        '')
        (pkgs.writeShellScriptBin "hermes-rollback" ''
          exec systemctl start hermes-rollback.service
        '')
        (pkgs.writeShellScriptBin "hermes-status" ''
          echo "== git =="; git -C ${cfg.repoDir} log --oneline -5
          echo "== generations =="; nix-env -p /nix/var/nix/profiles/system --list-generations
          echo "== last-known-good =="; cat ${cfg.stateDir}/last-known-good 2>/dev/null || echo none
          echo "== service =="; systemctl status hermes-agent --no-pager
        '')
      ];
  };
}
