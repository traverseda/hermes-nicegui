# Git-driven deployment + health-checked auto-rollback for Hermes.
#
# This is the heart of the "bot changes itself safely" story.
#
# How a change ships:
#   1. The bot (or a human) commits to the flake repo.
#   2. `hermes-deploy` runs `nixos-rebuild switch`, then runs all
#      health checks from the registry. If ANY fail, the deploy is
#      rolled back immediately via `nixos-rebuild switch --rollback`.
#      This lets the bot deploy a fix for a broken tunnel (the fix
#      ships, checks pass), while refusing to ship when it tries to
#      modify its core while already broken (deploy fails, rolls back).
#      No pre-deploy health block; the post-switch gate is the decision.
#   3. If the agent OR nicegui crashes after a deploy, systemd's OnFailure
#      chain triggers content-lane recovery, then a Nix generation rollback.
#   4. `hermes-rollback` does a manual git revert + roll back one generation.
#
# Health-check structure:
#   Each registered health check gets a dedicated oneshot systemd unit
#   (`hermes-health-<name>.service`). No timers, no polling, no watchdog —
#   health is only evaluated at deploy time and OnFailure. The structure
#   is extensible: add a new check by registering it in
#   `services.hermes-deploy.health.checks` — a new oneshot unit appears
#   automatically and is included in the deploy gate.
#
# Failure propagation chain (heartbeat-free, systemd-native):
#   hermes-agent OR hermes-nicegui CRASHES (exits non-zero)
#     -> OnFailure=hermes-content-recovery-run.service
#         (revert content-lane: skills/tools/MCP registrations, no Nix rebuild)
#           -> OnFailure=hermes-rollback-run.service
#               (roll back one Nix generation - last resort)
#
# No timers, no polling, no watchdog. Systemd handles the failure chain
# natively: OnFailure triggers whenever the unit exits non-zero, regardless
# of how long it stayed alive in between.
#
# Rollback layers (defence in depth):
#   * Git history of the flake repo - revert any self-edit.
#   * Nix generations - rolling back one generation returns to the previous
#     system state; the store keeps every referenced generation.
#   * Last-known-good generation - the deployScript and rollbackScript
#     maintain it; OnFailure recovery does NOT touch it (the generation
#     that shipped is the correct baseline to unwind from).
#   * Proxmox snapshots (pct snapshot) - external safety net, scripted in
#     scripts/deploy.sh.
#
# Submodule-aware rollback: vendor/hermes-agent, vendor/hermes-nicegui, and
# vendor/xaelWiki are real git submodules, tracked normally by ${cfg.repoDir}
# (no more excluding vendor/ from the ledger that was the previous design,
# and it meant a generation rollback could never actually undo a bad self-edit
# to the agent's own source). Every commit that produces a generation gets
# tagged `gen-<N>`; any rollback path (deploy-time auto-rollback,
# hermes-rollback) resets the repo to that tag AND runs `git
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

  # The ACTIVE generation - what is running right now - not the highest-
  # numbered one. `nix-env --rollback` does NOT create a new generation: it
  # only moves the `(current)` marker back while the highest number stays put.
  # So `list-generations | tail -n1` keeps returning the stale high number
  # after a rollback. Reading the system profile link is the only reliable
  # answer. NB: the link is `system-<N>-link` (no leading dash) - a pattern
  # expecting `-system-` silently never matches and returns empty.
  currentGeneration = ''
    readlink /nix/var/nix/profiles/system \
      | sed -n 's/.*system-\([0-9][0-9]*\)-link$/\1/p'
  '';

  # Health check registry — defaults plus any user additions.
  # If user overrides any check, it replaces the default by key name.
  # If user provides all five default keys (agent, tailnet, zerotier, tunnel, hindsight),
  # those completely replace the defaults (user has full control).
  healthChecks =
    let
      _defaults = {
        agent = {
          what = "hermes gateway (active + hermes doctor)";
          check = ''
            set -uo pipefail
            deadline=$((SECONDS + ${toString cfg.gracePeriod}))
            while ! systemctl is-active --quiet hermes-agent \
              && runuser -u ${config.services.hermes-agent.user} \
                 -- env HERMES_HOME=${config.services.hermes-agent.stateDir}/.hermes \
                    hermes doctor >/dev/null 2>&1; do
              if (( SECONDS >= deadline )); then exit 1; fi
              sleep 5
            done
          '';
        };
        tailnet = {
          what = "tailscaled network connectivity";
          check = "systemctl is-active --quiet tailscaled";
        };
        zerotier = {
          what = "zerotierone network connectivity";
          check = "systemctl is-active --quiet zerotierone";
        };
        tunnel = {
          what = "cloudflared tunnel connectivity";
          check = "systemctl is-active --quiet cloudflared-tunnel";
        };
        hindsight = {
          what = "hindsight API health";
          check = ''
            set -uo pipefail
            deadline=$((SECONDS + 90))
            while ! http_code=$(curl -sf --max-time 5 -o /dev/null -w "%{http_code}" http://127.0.0.1:8888/health 2>/dev/null || echo "000"); do
              if [ "$http_code" = "401" ] || [ "$http_code" = "200" ]; then break; fi
              if (( SECONDS >= deadline )); then exit 1; fi
              sleep 5
            done
          '';
        };
      };
      userChecks = cfg.health.checks;
      allUser = lib.all (_: true) (lib.attrNames _defaults);
      hasUserAll = lib.all (n: lib.hasAttr n userChecks) (lib.attrNames _defaults);
      hindsightEnabled = config ? services.hindsight && config.services.hindsight.enable;
    in
    if hasUserAll then userChecks
    else
      let
        withHindsight = _defaults // userChecks;
        withoutHindsight = (builtins.removeAttrs _defaults [ "hindsight" ]) // userChecks;
      in
      if hindsightEnabled then withHindsight else withoutHindsight;

  # Store path for each check script
  healthScripts = lib.mapAttrs (name: check:
    pkgs.writeShellScript "hermes-health-${name}" check.check
  ) healthChecks;

  # Bash health check invocations with counter variable.
  # Uses a counter (not bash array) to avoid ${#arr[@]} syntax which
  # Nix's raw string parser tries to interpret as a Nix interpolation.
  healthCheckInvocations = lib.concatStringsSep "\n" (
    lib.mapAttrsToList (name: script: ''
      if ! timeout ${toString cfg.gracePeriod} bash "${script}"; then
        failed_count=$((failed_count + 1))
      fi
    '') healthScripts
  );

  # ── Recovery guard ─────────────────────────────────────────────────
  # Function used inside ExecStart of recovery/rollback units (OnFailure
  # chain). Checks both must pass:
  #   1. currentGeneration parses to a number >= 1 from the nix profile link
  #   2. last-known-good exists AND is non-empty
  # If any check fails: exits 0 immediately (unit stays "active (exited)").
  # No escalation happens. For oneshot units with RemainAfterExit=true this
  # is the ONLY safe place to guard: ExecStartPre failure triggers OnFailure,
  # which is the exact escalation we want to block.
  recoveryGuardFn = ''
    _recovery_guard() {
      local current
      current=$(readlink /nix/var/nix/profiles/system \
        | sed -n 's/.*system-\([0-9][0-9]*\)-link$/\1/p')
      if [ -z "$current" ] || [ "$current" -lt 1 ] 2>/dev/null; then
        echo "recovery guard: cannot parse current gen - no valid rollback target" >&2
        exit 0
      fi
      if [ ! -f ${cfg.stateDir}/last-known-good ] || [ ! -s ${cfg.stateDir}/last-known-good ]; then
        echo "recovery guard: no last-known-good - recovery skipped" >&2
        exit 0
      fi
    }
  '';

  # ── Content-lane recovery hook ──────────────────────────────────────
  # Content revert command: try before spending a generation. Set by
  # modules/hermes-tools.nix to `hermes-tool revert`. Empty = no content lane.
  contentRecoveryScript = pkgs.writeShellScript "hermes-content-recovery" (
    lib.optionalString (cfg.contentRecovery != "") (
      ''
        ${recoveryGuardFn}
        _recovery_guard || true
      '' + cfg.contentRecovery
    )
  );

  # ── Rollback generation ─────────────────────────────────────────────
  # `nixos-rebuild switch --rollback` is the proper NixOS-native rollback
  # mechanism - it uses nix-env --rollback to find the previous generation,
  # then runs that generation's switch-to-configuration. This handles unit
  # ordering, activation scripts, and profile switching atomically; manual
  # nix-env --rollback leaves the system in an indeterminate state.
  rollbackGeneration = ''
    nixos-rebuild switch --rollback 2>&1 \
      | tee -a /var/log/hermes-deploy.log
  '';

  # ── Git sync functions ──────────────────────────────────────────────
  # Shared by deploy, rollback, and content recovery scripts.
  #
  # tag_gen: record which commit produced a generation, right after a switch
  # (regardless of whether it turns out healthy - this is a factual mapping,
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
  # that commit's tree (a plain, well-defined git operation - submodules are
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
        log "FATAL: no gen-$gen tag - git ledger/vendor/ NOT moved, only Nix rolled back"
        log "Next deploy will re-package the newer source and re-propose broken config"
        log "This unit MUST exit 1 to prevent permanent Nix-source misalignment"
        exit 1
      fi
    }
  '';

  # ── Deploy script ────────────────────────────────────────────────────
  # Build, switch, health-check, rollback if unhealthy.
  deployScript = pkgs.writeShellScript "hermes-deploy-script" ''
    set -euo pipefail

    log() { echo "[hermes-deploy] $*"; }
    ${gitSyncFns}

    # No git remote: the checkout in ${cfg.repoDir} IS the source of truth.
    # The operator pushes a fresh copy (scripts/deploy.sh rsyncs it), and the
    # bot commits directly here then rebuilds. Record any uncommitted edits in
    # the local ledger so the deploy is always a committed state (rollback
    # = git reset HEAD~1).
    # flock prevents the 15min auto-commit timer from writing a partial config
    # snapshot mid-commit (race: both touch the same git repo).
    (
      flock -w 10 200 || exit 1
      git -C ${cfg.repoDir} config user.name "hermes-deploy"
      git -C ${cfg.repoDir} config user.email "hermes-deploy@localhost"
      git -C ${cfg.repoDir} add -A
      git -C ${cfg.repoDir} commit -q -m "deploy $(date -Is)" 2>/dev/null || true
    ) 200>/tmp/hermes-deploy-git.lock
    # Submodules: git submodule update does not touch the same index file as
    # add/commit, so it does not need the lock.
    git -C ${cfg.repoDir} submodule update --init --recursive 2>/dev/null || true

    PREV_GEN=$(${currentGeneration})
    log "previous generation: $PREV_GEN"

    # Pre-deploy validation: fail fast on eval errors, vendor narHash drift,
    # or syntax mistakes instead of wasting time building and then failing.
    # This is the critical step — vendor edits that forget `nix flake lock`
    # will get caught here with a clear error message.
    log "pre-deploy validation (flake check --no-build)"
    if ! nix flake check --no-build "$cfg.repoDir" 2>&1 | tee -a /var/log/hermes-deploy.log; then
      log "pre-deploy validation FAILED — aborting deploy"
      log "Run 'nix flake check --no-build' to see the full error."
      log "Common fix: cd vendor/<submodule> && git add -A && git commit -q -m 'fix: <msg>' && nix flake lock --update-input <input>"
      exit 1
    fi
    log "validation passed"

    log "building & switching generation (max-jobs ${toString cfg.maxJobs}, cores ${toString cfg.cores})"
    nixos-rebuild switch --max-jobs ${toString cfg.maxJobs} --cores ${toString cfg.cores} --flake "${cfg.repoDir}#${cfg.flakeAttr}" 2>&1 \
      | tee -a /var/log/hermes-deploy.log

    CUR_GEN=$(${currentGeneration})
    tag_gen "$CUR_GEN"

    # Health check gate: run ALL health checks; collect failures.
    # Strict all-pass: any failure causes a rollback (the deploy
    # "deserves to fail"). Uses a counter variable to avoid bash array
    # length syntax that Nix raw string parser mis-interprets.
    failed_count=0
    ${healthCheckInvocations}

    if [ "$failed_count" -eq 0 ]; then
      echo "deploy OK (generation $CUR_GEN)"
      echo "$CUR_GEN" > ${cfg.stateDir}/last-known-good
      exit 0
    fi

    log "health checks failed: ${lib.concatStringsSep ", " (lib.attrNames healthChecks)}"

    # Content-recovery retry (cheap fix before spending a generation)
    ${lib.optionalString (cfg.contentRecovery != "") ''
      log "AGENT/NICEGUI REPORTED UNHEALTHY — trying content recovery first -- ${cfg.contentRecovery}"
      if timeout ${toString cfg.gracePeriod} bash ${contentRecoveryScript}; then
        sleep 10
        # Re-run all health checks after content recovery
        failed2_count=0
        ${healthCheckInvocations}
        if [ "$failed2_count" -eq 0 ]; then
          echo "content recovery restored health (generation $CUR_GEN)"
          echo "$CUR_GEN" > ${cfg.stateDir}/last-known-good
          exit 0
        fi
        log "content recovery did not fix"
      fi
    ''}

    # Rollback one generation
    log "AGENT/NICEGUI REPORTED UNHEALTHY — rolling back one generation to $PREV_GEN"
    ${rollbackGeneration}
    ROLLED_TO=$(${currentGeneration})
    if [ "$ROLLED_TO" != "$PREV_GEN" ]; then
      log "WARNING: expected generation $PREV_GEN but landed on $ROLLED_TO"
    else
      log "rolled back from $CUR_GEN to $PREV_GEN"
    fi
    sync_repo_to_gen "$ROLLED_TO"
    exit 1
  '';

  # ── Rollback script ──────────────────────────────────────────────────
  rollbackScript = pkgs.writeShellScript "hermes-rollback-script" ''
    set -uo pipefail
    log() { echo "[hermes-rollback] $*"; }
    ${recoveryGuardFn}
    ${gitSyncFns}

    _recovery_guard || true

    log "rolling back system by one generation"
    # Do NOT use `set -e` here: a single-generation system has nothing to
    # rollback to, and we must still run the git-sync step regardless.
    ${rollbackGeneration}

    # Determine which generation we rolled back to. If nix-env rollback succeeded,
    # currentGeneration gives the target. If it failed (single-gen system), we
    # compute it as the previous tagged generation in git (gen-N -> gen-(N-1)).
    ROLLED_TO=$(${currentGeneration})
    if [ -n "$ROLLED_TO" ] && git -C ${cfg.repoDir} rev-parse -q --verify "refs/tags/gen-$ROLLED_TO" >/dev/null 2>&1; then
      log "rolled to generation $ROLLED_TO"
    else
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

    # Post-rollback verification: mark known-good only if checks pass.
    sleep 10
    rollback_ok=true
    failed_count=0
    ${healthCheckInvocations}
    if [ "$failed_count" -gt 0 ]; then
      rollback_ok=false
    fi
    if $rollback_ok; then
      echo "$ROLLED_TO" > ${cfg.stateDir}/last-known-good
    else
      log "WARNING: rolled back to generation $ROLLED_TO but health checks still fail"
    fi
  '';

  # ── Herme-status health checker ──────────────────────────────────────
  # Generates the health section dynamically at Nix-eval time so it always
  # covers whatever checks the current generation knows about.
  hermesStatusHealth = pkgs.writeShellScript "hermes-status-health" ''
    echo "== health =="
    failed_count=0
    ${healthCheckInvocations}
    if [ "$failed_count" -eq 0 ]; then
      echo "all checks passed"
    else
      echo "failed: ${lib.concatStringsSep ", " (lib.attrNames healthChecks)}"
    fi
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
        Command run when the agent/nicegui is unhealthy, BEFORE falling back
        to a generation rollback (in both the deploy script and the OnFailure
        chain). Set by the content-store module (services.hermes-tools) to
        `hermes-tool revert`, which restores the last-known-good
        skills/tools/config from git at content granularity - no Nix rebuild.
        Empty = no content lane.
      '';
    };

    # ── Health check registry ──────────────────────────────────────────
    # Declared health checks; each gets a dedicated oneshot unit
    # (`hermes-health-<name>.service`) and is invoked during the deploy
    # gate. Host configs can add new checks (e.g. `ha-reachable`) just
    # by adding an entry. No timers - health is only polled at deploy
    # time.
    health.checks = lib.mkOption {
      type = lib.types.attrsOf (lib.types.submodule {
        options = {
          what = lib.mkOption {
            type = lib.types.str;
            description = "Human-readable check description.";
          };
          check = lib.mkOption {
            type = lib.types.str;
            description = "Bash command; exit 0 = healthy.";
          };
        };
      });
      default = {};
      description = ''
        Health check registry: attrs of
        {what, check}. Each entry generates a oneshot systemd unit
        hermes-health-<name>.service and is invoked during the deploy gate.
        Add entries to hang future health checks off this structure.
        Defaults (agent, tailnet, zerotier, tunnel, hindsight) always present.
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
        headroom because the kernel reclaims cache and swaps before OOM -
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
        Systemd MemoryMax for switch/build transients (deploy, rollback,
        OnFailure recovery). 3G = half of RAM: enough for a serial build
        with evaluation, small enough that even a runaway builder cannot
        evict the resident stack faster than the kernel can reclaim.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    # ── Git ownership: the worktree is hermes-owned (the bot must edit it)
    #    but git is ALSO run by root (deploy/rollback services,
    #    hermes-status, and every activation). git >=2.35 refuses to operate on
    #    a repo owned by another user (CVE-2022-24765), so without an explicit
    #    exception every root-run git command fails with "detected dubious
    #    ownership". `sync_repo_to_gen`'s `git submodule update` runs INSIDE
    #    each vendor/* submodule as root too - each one needs its own
    #    safe.directory entry, or a rollback silently fails to sync vendor/*
    #    with the same "dubious ownership" error. Scope the exception to
    #    exactly the repos this module manages - never `safe.directory = *`,
    #    which would let the sandboxed bot trick root into running git
    #    hooks. --------------------------------------
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
    # - so the deploy/rollback git phases silently no-op (`|| true`), the bot
    # has nowhere to commit, and hermes-status says "not a git repository".
    # Runs on EVERY activation: re-claims ownership (operator rsyncs land as
    # the build machine's uid, unknown on the box) and git-inits once.
    #
    # vendor/ IS tracked and hermes-owned like the rest of the tree - the bot
    # edits vendored source there, and the ledger needs the resulting gitlink
    # commits for `sync_repo_to_gen` (above) to be able to move vendor/*
    # submodules back in step with a rolled-back generation. Only state/
    # (root-owned rollback bookkeeping: last-known-good, gen-<N> tags apply to
    # the whole repo so they're fine either way) is excluded - a `git reset
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
      # Git submodules (hermes-agent, hermes-nicegui) are NOT rsync-ed to the box -
      # they are tracked separately in the flake repo. Initialize them now so
      #  the hermes-agent binary can find its venv for its bundled Python deps.
      ${lib.getExe pkgs.git} -C ${cfg.repoDir} submodule update --init --recursive 2>/dev/null || true
      # Ledger hygiene: state/ (root-owned rollback bookkeeping) must NOT be
      # tracked, so a `git reset --hard` in sync_repo_to_gen never touches it.
      grep -q '^state/$' ${cfg.repoDir}/.git/info/exclude 2>/dev/null \
        || printf 'state/\n' >> ${cfg.repoDir}/.git/info/exclude
      ${lib.getExe pkgs.git} -C ${cfg.repoDir} rm -r --cached state 2>/dev/null || true
    '';

    # All systemd services in one merged definition: dynamically-generated
    # health check units + core deploy/rollback/onFailure units.
    systemd.services = let
      healthService = name: check: {
        "${"hermes-health-${name}"}" = {
          description = "health check: ${check.what}";
          wantedBy = [];
          path = [ config.system.path ];
          serviceConfig = {
            Type = "oneshot";
            RemainAfterExit = true;
            ExecStart = "${pkgs.bash}/bin/bash ${healthScripts.${name}}";
            MemoryMax = cfg.switchMemoryMax;
          };
        };
        "_unused" = { }; # placeholder for lib.mapAttrs return
      };
      defaultHealthUnits = builtins.listToAttrs (
        lib.mapAttrsToList (name: check: {
          name = "hermes-health-${name}";
          value = {
            description = "health check: ${check.what}";
            wantedBy = [];
            path = [ config.system.path ];
            serviceConfig = {
              Type = "oneshot";
              RemainAfterExit = true;
              ExecStart = "${pkgs.bash}/bin/bash ${healthScripts.${name}}";
              MemoryMax = cfg.switchMemoryMax;
            };
          };
        }) healthChecks
      );
      coreUnits = {
        hermes-deploy.description = "Deploy Hermes from git and health-check (with auto-rollback)";
        hermes-deploy.wantedBy = [];
        hermes-deploy.path = [ config.system.path ];
        hermes-deploy.serviceConfig = {
          Type = "oneshot";
          # Fork into new session -> detached from systemd -> survives
          # activation cycle that would otherwise kill this unit mid-execution.
          # ">/dev/null 2>&1 < /dev/null" closes fds so the detached process
          # doesn't hold onto the old generation's ptmx/sockets.
          ExecStart = "${pkgs.coreutils}/bin/setsid ${deployScript} >/dev/null 2>&1 < /dev/null";
          TimeoutStartSec = 0;
          MemoryMax = cfg.switchMemoryMax;
        };
        hermes-rollback.description = "Roll back Hermes to the previous git/system generation";
        hermes-rollback.wantedBy = [];
        hermes-rollback.path = [ config.system.path ];
        hermes-rollback.serviceConfig = {
          Type = "oneshot";
          ExecStart = "${pkgs.coreutils}/bin/setsid ${rollbackScript} >/dev/null 2>&1 < /dev/null";
          TimeoutStartSec = 0;
          MemoryMax = cfg.switchMemoryMax;
        };
        "hermes-content-recovery-run".description = "Content-lane recovery (runs when hermes-agent/nicegui crashes)";
        "hermes-content-recovery-run".wantedBy = [];
        "hermes-content-recovery-run".path = [ config.system.path ];
        "hermes-content-recovery-run".serviceConfig = {
          Type = "oneshot";
          ExecStart = contentRecoveryScript;
          RemainAfterExit = true;
          MemoryMax = cfg.switchMemoryMax;
        };
        "hermes-content-recovery-run".onFailure = [ "hermes-rollback-run.service" ];
        "hermes-rollback-run".description = "Nix generation rollback (runs when content recovery fails)";
        "hermes-rollback-run".wantedBy = [];
        "hermes-rollback-run".path = [ config.system.path ];
        "hermes-rollback-run".serviceConfig = {
          Type = "oneshot";
          ExecStart = rollbackScript;
          MemoryMax = cfg.switchMemoryMax;
        };
        "hermes-nicegui-restart".description = "Simple restart for hermes-nicegui crashes (no Nix rollback)";
        "hermes-nicegui-restart".wantedBy = [];
        "hermes-nicegui-restart".path = [ config.system.path ];
        "hermes-nicegui-restart".serviceConfig = {
          Type = "oneshot";
          ExecStart = pkgs.writeShellScript "hermes-nicegui-restart-script" ''
            systemctl restart hermes-nicegui
          '';
        };
        "hermes-agent".onFailure =
          lib.mkIf (cfg.contentRecovery == "") (lib.mkForce [ "hermes-rollback-run.service" ]);
        "hermes-nicegui".onFailure =
          (lib.mkForce [ "hermes-nicegui-restart.service" ]);
      };
    in
    defaultHealthUnits // coreUnits;

    # Keep enough generations that rollback always has somewhere to go.
    # Weekly GC with a 14-day window preserves at least two weeks of
    # rollback history for a system that may create only 1-2 generations/day.
    nix.gc = {
      automatic = true;
      dates = "weekly";
      options = "--delete-older-than 14d";
    };

    # git is a hard dependency of the deploy/rollback scripts and of
    # `hermes-status` - they all run git against the flake repo. It must be on
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
          echo "== services =="
          systemctl is-active --no-pager hermes-agent 2>&1 || true
          systemctl is-active --no-pager hermes-nicegui 2>&1 || true
          echo "== health =="
          ${hermesStatusHealth}
        '')
      ];
  };
}
