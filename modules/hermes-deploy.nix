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
#
# Submodule-aware rollback: vendor/hermes-agent, vendor/hermes-nicegui, and
# vendor/xaelWiki are real git submodules, tracked normally by ${cfg.repoDir}
# (no more excluding vendor/ from the ledger — that was the previous design,
# and it meant a generation rollback could never actually undo a bad self-edit
# to the agent's own source). Every commit that produces a generation gets
# tagged `gen-<N>`; any rollback path (deploy-time auto-rollback,
# hermes-rollback, the watchdog) resets the repo to that tag AND runs `git
# submodule update` afterwards, so vendor/* checkouts always move in lockstep
# with the Nix generation they belong to. Without the submodule sync step, the
# NEXT deploy would just re-propose the same bad vendored code, since `git add
# -A` picks up whatever commit each submodule currently has checked out.

{
  config,
  pkgs,
  lib,
  ...
}:

let
  cfg = config.services.hermes-deploy;

  # The ACTIVE generation — what is running right now — not the highest-
  # numbered one. `nix-env --rollback` does NOT create a new generation: it
  # only moves the `(current)` marker back while the highest number stays put.
  # So `list-generations | tail -n1` keeps returning the stale high number
  # after a rollback and the watchdog would roll back PAST last-known-good
  # forever. Reading the system profile link is the only reliable answer.
  # NB: the link is `system-<N>-link` (no leading dash) — a pattern expecting
  # `-system-` silently never matches and returns empty, which breaks the
  # watchdog's rollback guard AND writes an empty last-known-good.
  currentGeneration = ''
    readlink /nix/var/nix/profiles/system \
      | sed -n 's/.*system-\([0-9][0-9]*\)-link$/\1/p'
  '';

  # Public-hostname probes: the deploy+watchdog health check must also prove
  # the published hostname is alive, not just the gateway. PASS = any HTTP
  # < 500 (2xx/3xx/401/403/404 all prove the tunnel connector + origin are
  # alive); HTTP >= 500 (530/502/521/522) or a network failure/timeout = FAIL.
  # Empty healthCheckUrls skips the probes (hermetic test environments). NB:
  # never use `curl -f` — it exits non-zero on ANY HTTP >= 400, but 4xx is a
  # PASS here.
  healthCheckProbeScript = pkgs.writeShellScript "hermes-health-probe" ''
    for url in ${lib.concatStringsSep " " cfg.healthCheckUrls}; do
      code="$(${pkgs.curl}/bin/curl -sS --max-time 15 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null)" || code="000"
      if [ "$code" = "000" ]; then
        echo "health probe FAIL: $url unreachable (network error or timeout)" >&2
        exit 1
      fi
      if [ "$code" -ge 500 ]; then
        echo "health probe FAIL: $url returned HTTP $code" >&2
        exit 1
      fi
    done
  '';

  # set -e is REQUIRED: the default healthCheck ends with `hermes doctor
  # >/dev/null 2>&1` as its last command; without it a doctor failure would be
  # masked by the probe's exit status. Gateway health stays required.
  healthCheckScript = pkgs.writeShellScript "hermes-health-check" ''
    set -e
    ${cfg.healthCheck}
    bash ${healthCheckProbeScript}
  '';

  # Content-lane recovery hook: when the agent is unhealthy, try reverting the
  # fast-path content store (skills/tools/MCP registrations) BEFORE spending a
  # Nix generation. Set by modules/hermes-tools.nix; empty = no content lane.
  contentRecoveryScript = lib.optionalString (cfg.contentRecovery != "") (
    pkgs.writeShellScript "hermes-content-recovery" cfg.contentRecovery
  );

  # Roll back exactly one generation WITHOUT nixos-rebuild. nixos-rebuild's
  # `--rollback` self-locates by building itself from <nixpkgs/nixos>, which
  # needs NIX_PATH (nixpkgs + nixos-config) — a flake-only box doesn't have
  # those, so it fails with "file 'nixos-config' was not found". The low-level
  # equivalent is identical in effect: `nix-env --rollback` moves the system
  # profile marker back one generation (it does NOT create a new generation),
  # then we re-run that generation's own switch-to-configuration.
  #
  # switch-to-configuration is run as a DETACHED transient unit: it treats any
  # active-but-unwanted unit (wantedBy=[]) as "to stop", and hermes-rollback /
  # hermes-deploy / hermes-watchdog are exactly that while they run the switch
  # — a foreground switch gets TERM'd mid-flight (observed: "stopping the
  # following units: hermes-rollback.service"). As a transient unit the switch
  # survives that stop.
  rollbackGeneration = ''
    # nix-env --rollback returns failure on a single-generation system —
    # that is fine; the git-sync step below is what matters.
    nix-env -p /nix/var/nix/profiles/system --rollback 2>&1 \
      | tee -a /var/log/hermes-deploy.log \
      || true
    /nix/var/nix/profiles/system/bin/switch-to-configuration switch 2>&1 \
      | tee -a /var/log/hermes-deploy.log \
      || true
  '';

  # Bash functions shared by all three scripts below (deploy/rollback/
  # watchdog). Each script defines its own `log()` first; these assume it
  # exists.
  #
  # tag_gen: record which commit produced a generation, right after a switch
  # (regardless of whether it turns out healthy — this is a factual mapping,
  # not a health judgement; last-known-good tracks health separately).
  #
  # sync_repo_to_gen: the git-ledger half of a rollback. A Nix-level rollback
  # (rollbackGeneration above) only restores the system closure; it does
  # nothing to ${cfg.repoDir}'s working tree. Without also moving the outer
  # repo (and therefore every vendor/* submodule gitlink in it) back to the
  # commit that produced the target generation, the working tree stays at the
  # newer/bad commit and the next deploy's `git add -A` would just re-propose
  # the exact code that was just rolled back from. `git submodule update`
  # after the reset moves each vendor/* submodule to the commit recorded in
  # that commit's tree (a plain, well-defined git operation — submodules are
  # ordinary gitlinks).
  gitSyncFns = ''
    tag_gen() {
      git -C ${cfg.repoDir} tag -f "gen-$1" HEAD >/dev/null 2>&1 || true
    }
    sync_repo_to_gen() {
      local gen="$1"
      if git -C ${cfg.repoDir} rev-parse -q --verify "refs/tags/gen-$gen" >/dev/null 2>&1; then
        git -C ${cfg.repoDir} reset --hard "gen-$gen"
        git -C ${cfg.repoDir} submodule update --init --recursive 2>/dev/null || true
        log "synced git ledger + vendor/ submodules to gen-$gen"
      else
        log "WARNING: no gen-$gen tag recorded — git ledger and vendor/ checkouts were NOT moved and may not match the restored generation"
      fi
    }
  '';

  deployScript = pkgs.writeShellScript "hermes-deploy-script" ''
    set -euo pipefail

    log() { echo "[hermes-deploy] $*"; }
    ${gitSyncFns}

    # No git remote: the checkout in ${cfg.repoDir} IS the source of truth.
    # The operator pushes a fresh copy (scripts/deploy.sh rsyncs it), and the
    # bot commits directly here then rebuilds. Record any uncommitted edits in
    # the local ledger so the deploy is always a committed state (rollback
    # = git reset HEAD~1).
    git -C ${cfg.repoDir} config user.name "hermes-deploy"
    git -C ${cfg.repoDir} config user.email "hermes-deploy@localhost"
    git -C ${cfg.repoDir} add -A
    git -C ${cfg.repoDir} commit -q -m "deploy $(date -Is)" 2>/dev/null || true
    # Initialize any vendor/* submodule that has never been checked out on
    # this box (fresh clone / newly added submodule). Does NOT touch an
    # already-initialized submodule's checked-out commit — `git add -A` above
    # already captured whatever each submodule had checked out (which is why
    # a code change must be committed INSIDE the submodule first: an
    # uncommitted edit there is invisible to `git add -A` and never ships).
    git -C ${cfg.repoDir} submodule update --init --recursive 2>/dev/null || true

    PREV_GEN=$(${currentGeneration})
    log "previous generation: $PREV_GEN"

    log "building & switching generation"
    nixos-rebuild switch --flake "${cfg.repoDir}#${cfg.flakeAttr}" 2>&1 \
      | tee -a /var/log/hermes-deploy.log

    CUR_GEN=$(${currentGeneration})
    tag_gen "$CUR_GEN"

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
    ${rollbackGeneration}
    ROLLED_TO=$(${currentGeneration})
    if [ "$ROLLED_TO" != "$PREV_GEN" ]; then
      log "WARNING: expected to be at generation $PREV_GEN but am at $ROLLED_TO"
    else
      log "rolled back from generation $CUR_GEN to $PREV_GEN"
    fi
    # Move the outer repo (and every vendor/* submodule) back in step with
    # the generation we actually landed on.
    sync_repo_to_gen "$ROLLED_TO"
    exit 1
  '';

  rollbackScript = pkgs.writeShellScript "hermes-rollback-script" ''
    set -uo pipefail
    log() { echo "[hermes-rollback] $*"; }
    ${gitSyncFns}

    log "rolling back system by one generation"
    # Do NOT use `set -e` here: a single-generation system has nothing to rollback
    # to, and we must still run the git-sync step regardless.
    ${rollbackGeneration}

    # Determine which generation we rolled back to. If nix-env rollback succeeded,
    # currentGeneration gives the target. If it failed (single-gen system), we
    # compute it as the previous tagged generation in git (gen-N -> gen-(N-1)).
    ROLLED_TO=$(${currentGeneration})
    if [ -n "$ROLLED_TO" ] && git -C ${cfg.repoDir} rev-parse -q --verify "refs/tags/gen-$ROLLED_TO" >/dev/null 2>&1; then
      log "rolled to generation $ROLLED_TO"
    else
      # No generation info from nix profile; find the oldest gen- tag in git
      oldest_tag=$(git -C ${cfg.repoDir} tag -l 'gen-*' | sed 's/^gen-//' | sort -n | head -1)
      if [ -n "$oldest_tag" ]; then
        ROLLED_TO="$oldest_tag"
        log "falling back to oldest gen tag: gen-$ROLLED_TO"
      else
        log "WARNING: no gen tags found; cannot sync git ledger to a generation"
        exit 1
      fi
    fi
    sync_repo_to_gen "$ROLLED_TO"

    # Mark this generation known-good only if the agent reports healthy again.
    sleep 10
    if timeout ${toString cfg.gracePeriod} bash ${healthCheckScript}; then
      echo "$ROLLED_TO" > ${cfg.stateDir}/last-known-good
    fi
  '';

  watchdogScript = pkgs.writeShellScript "hermes-watchdog-script" ''
    set -euo pipefail
    log() { echo "[hermes-watchdog] $*"; }
    ${gitSyncFns}

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
    ${rollbackGeneration}
      ROLLED_TO=$(${currentGeneration})
      sync_repo_to_gen "$ROLLED_TO"
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
      description = ''
        Bash command; exit 0 = healthy. Runs as root and should ask Hermes
        itself. The healthCheckUrls probes are appended to this script whenever
        it runs.
      '';
    };

    # ── R2 resource hardening (ticket t_68443fdf, 2026-08-26 incidents) ──
    minAvailableMemMb = lib.mkOption {
      type = lib.types.int;
      default = 2048;
      description = ''
        Pre-switch memory gate floor in MB. Headroom = MemAvailable +
        SwapFree. Full uncapped rebuilds wedged this box on 2026-08-26
        (7.8G RAM, load 9.37, two build workers died, host bounced). A
        --max-jobs 1 rebuild + evaluation needs roughly 1-2GB headroom;
        2048MB guarantees the switch can complete beside the resident stack
        (hindsight cross-encoder alone holds ~2.1G). SwapFree counts as
        headroom because the kernel reclaims cache and swaps before OOM —
        conservative given thrash.
      '';
    };

    maxJobs = lib.mkOption {
      type = lib.types.int;
      default = 1;
      description = ''
        nix build --max-jobs on the deploy switch path. One derivation at a
        time so a closure-touching change can never fork enough builders to
        starve the box again (2026-08-26 wedge).
      '';
    };

    cores = lib.mkOption {
      type = lib.types.int;
      default = 2;
      description = ''
        nix build --cores on the deploy switch path: per-builder make -j
        cap. Box has 2 cores, so this allows one builder to use them all
        but no more.
      '';
    };

    switchMemoryMax = lib.mkOption {
      type = lib.types.str;
      default = "3G";
      description = ''
        Systemd MemoryMax for every switch/build transient (deploy,
        rollback, watchdog). 3G = half of RAM: enough for a serial build
        with evaluation, small enough that even a runaway builder cannot
        evict the resident stack faster than the kernel can reclaim.
      '';
    };

    healthCheckUrls = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "https://hermes.0u0.ca/" ];
      description = "Public http(s) URLs the deploy+watchdog health check probes; PASS = any HTTP response < 500 (2xx/3xx/401/403/404 all prove the tunnel connector + origin are alive); HTTP >= 500 (530/502/521/522) or network failure/timeout = FAIL. Empty list = probes skipped (hermetic test environments).";
    };

    watchdogInterval = lib.mkOption {
      type = lib.types.str;
      default = "15min";
      description = "How often the watchdog re-checks gateway health.";
    };
  };

  config = lib.mkIf cfg.enable {
    # ── Git ownership: the worktree is hermes-owned (the bot must edit it)
    #    but git is ALSO run by root (deploy/rollback/watchdog services,
    #    hermes-status, and every activation). git ≥2.35 refuses to operate on
    #    a repo owned by another user (CVE-2022-24765), so without an explicit
    #    exception every root-run git command fails with "detected dubious
    #    ownership". `sync_repo_to_gen`'s `git submodule update` runs INSIDE
    #    each vendor/* submodule as root too — each one needs its own
    #    safe.directory entry, or a rollback silently fails to sync vendor/*
    #    with the same "dubious ownership" error. Scope the exception to
    #    exactly the repos this module manages — never `safe.directory = *`,
    #    which would let the sandboxed bot trick root into running git
    #    hooks. ──────────────────────────────────────────────────────────
    environment.etc."gitconfig" = {
      text = ''
        [safe]
          directory = ${cfg.repoDir}
          directory = ${cfg.repoDir}/vendor/hermes-agent
          directory = ${cfg.repoDir}/vendor/hermes-nicegui
          directory = ${cfg.repoDir}/vendor/xaelWiki
          ${lib.optionalString (config ? services.hermes-tools) "directory = ${config.services.hermes-tools.contentDir}"}
      '';
    };

    # Make ${cfg.repoDir} a real git checkout the bot can edit and roll back
    # against. scripts/deploy.sh rsyncs the working tree WITHOUT `.git` (the
    # box keeps its own local ledger) and nothing on the box ever creates one
    # — so the deploy/rollback git phases silently no-op (`|| true`), the bot
    # has nowhere to commit, and hermes-status says "not a git repository".
    # Runs on EVERY activation: re-claims ownership (operator rsyncs land as
    # the build machine's uid, unknown on the box) and git-inits once.
    #
    # vendor/ IS tracked and hermes-owned like the rest of the tree — the bot
    # edits vendored source there, and the ledger needs the resulting gitlink
    # commits for `sync_repo_to_gen` (above) to be able to move vendor/*
    # submodules back in step with a rolled-back generation. Only state/
    # (root-owned rollback bookkeeping: last-known-good, gen-<N> tags apply to
    # the whole repo so they're fine either way) is excluded — a `git reset
    # --hard` must never touch root-owned files the bot can't write, and
    # state/last-known-good must survive a reset that undoes everything else.
    system.activationScripts.hermes-deploy-repo = lib.stringAfter [ "users" ] ''
      mkdir -p ${cfg.repoDir}
      chown ${config.services.hermes-agent.user}:${config.services.hermes-agent.group} ${cfg.repoDir}
      if [ ! -d ${cfg.repoDir}/.git ]; then
        echo "hermes-deploy: initialising git repo in ${cfg.repoDir}"
        ${lib.getExe pkgs.git} -C ${cfg.repoDir} init -q
        ${lib.getExe pkgs.git} -C ${cfg.repoDir} config user.name "hermes-deploy"
        ${lib.getExe pkgs.git} -C ${cfg.repoDir} config user.email "hermes-deploy@localhost"
        ${lib.getExe pkgs.git} -C ${cfg.repoDir} add -A 2>/dev/null || true
        ${lib.getExe pkgs.git} -C ${cfg.repoDir} commit -q -m "initial import from operator deploy" 2>/dev/null || true
      fi
      # Git submodules (hermes-agent, hermes-nicegui) are NOT rsync-ed to the box —
      # they are tracked separately in the flake repo. Initialize them now so
      # the hermes-agent binary can find its venv at vendor/hermes-agent/.venv.
      ${lib.getExe pkgs.git} -C ${cfg.repoDir} submodule update --init --recursive 2>/dev/null || true
      # Ledger hygiene: state/ (root-owned rollback bookkeeping) must NOT be
      # tracked, so a `git reset --hard` in sync_repo_to_gen never touches it.
      grep -q '^state/$' ${cfg.repoDir}/.git/info/exclude 2>/dev/null \
        || printf 'state/\n' >> ${cfg.repoDir}/.git/info/exclude
      ${lib.getExe pkgs.git} -C ${cfg.repoDir} rm -r --cached state 2>/dev/null || true
    '';

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
          echo "== vendor/ =="
          git -C ${cfg.repoDir} submodule status --recursive 2>/dev/null || echo "(no submodules)"
          echo "== generations =="; nix-env -p /nix/var/nix/profiles/system --list-generations
          echo "== last-known-good =="; cat ${cfg.stateDir}/last-known-good 2>/dev/null || echo none
          RUNNING_GEN=$(${currentGeneration})
          RUNNING_TAG="gen-$RUNNING_GEN"
          HEAD_COMMIT=$(git -C ${cfg.repoDir} rev-parse HEAD 2>/dev/null || echo unknown)
          TAG_COMMIT=$(git -C ${cfg.repoDir} rev-parse "$RUNNING_TAG" 2>/dev/null || echo unknown)
          if [ "$HEAD_COMMIT" = "$TAG_COMMIT" ]; then
            echo "== drift == none: git HEAD matches the running generation ($RUNNING_TAG)"
          else
            echo "== drift == WARNING: git HEAD is ahead of/different from the running generation."
            echo "  running generation $RUNNING_GEN was built from: $RUNNING_TAG -> $TAG_COMMIT"
            echo "  git HEAD is currently at:                        $HEAD_COMMIT"
            echo "  run hermes-deploy to build+switch to HEAD, or check what's pending with: git -C ${cfg.repoDir} log --oneline $RUNNING_TAG..HEAD"
          fi
          echo "== service =="; systemctl status hermes-agent --no-pager
        '')
      ];
  };
}
