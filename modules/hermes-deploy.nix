# Git-driven deployment + health-checked auto-rollback for Hermes.
#
# This is the heart of the "bot changes itself safely" story.
#
# How a change ships:
#   1. The bot (or a human) commits to the flake repo.
#   2. `hermes-deploy` pulls the repo, gates on a memory-headroom check, runs
#      `nixos-rebuild switch`, and health-checks the gateway — by asking the
#      agent itself (hermes doctor) AND probing the public hostnames
#      (healthCheckUrls). A bad deploy is rolled back automatically (one
#      generation).
#   3. A watchdog timer keeps checking; if a critical unit is down, or the
#      agent reports unhealthy and the current generation is newer than the
#      last-known-good generation, it rolls back — at most ONE generation per
#      check, so it can never jump past generations and converges on the last
#      good state one step at a time.
#   4. `hermes-rollback` does a git reset (repo + every vendor/* submodule)
#      + roll back one generation.
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
# (vendor/ is NOT excluded from the ledger — a generation rollback must be
# able to undo a bad self-edit to the agent's own source, not just Nix
# config). Every commit that produces a generation gets tagged `gen-<N>`; any
# rollback path (deploy-time auto-rollback, hermes-rollback, the watchdog)
# resets the repo to that tag AND runs `git submodule update` afterwards, so
# vendor/* checkouts always move in lockstep with the Nix generation they
# belong to. Without the submodule sync step, the NEXT deploy would just
# re-propose the same bad vendored code, since `git add -A` picks up whatever
# commit each submodule currently has checked out.
#
# Resource + self-kill hardening (ported from real incidents, 2026-08):
#   * A full uncapped rebuild wedged the box once (load 9.37, two build
#     workers died, host bounced) — every switch/build transient below is
#     MemoryMax-capped, serial (--max-jobs 1), and gated by a pre-switch
#     memory-headroom check (memoryGate) that refuses to even start a build
#     under pressure.
#   * `nixos-rebuild switch` stops any unit whose definition changed —
#     INCLUDING the unit that is currently running the switch, if that unit
#     is hermes-deploy/hermes-rollback/hermes-watchdog itself. A foreground
#     switch gets TERM'd mid-flight. deploy/rollback launch as DETACHED,
#     NAMED, memory-capped transients for exactly this reason; the watchdog's
#     rollback path does the same internally (watchdogRollback) since
#     hermes-watchdog.service itself runs in the foreground (timer-driven).
#   * A deploy that changes the flake but fails to also keep the box publicly
#     reachable is still a failed deploy: healthCheckUrls probes the actual
#     public hostname(s), not just the local gateway process (a generation
#     once shipped with no cloudflared unit and 530'd for ~20min while the
#     internal health check still passed).

{
  config,
  pkgs,
  lib,
  ...
}:

let
  cfg = config.services.hermes-deploy;

  # systemd-run transient units do NOT inherit the invoking unit's
  # Environment=PATH — they get a minimal default PATH (observed: a detached
  # deploy died "git: command not found"). Every generated script that can
  # run inside a transient unit must export its own PATH.
  # /run/current-system/sw re-points to the active generation (new tools
  # after a switch); the build-time system.path store path is immutable
  # insurance for pre-switch steps. git is pinned explicitly too since it is
  # not guaranteed in system-path on every future generation.
  pathExport = ''
    export PATH="${config.system.path}/bin:${pkgs.git}/bin:/run/current-system/sw/bin:$PATH"
  '';

  # Units that gate a generation's health. hermes-dashboard / hermes-nicegui
  # are only critical when enabled (the VM test fixture runs without them).
  criticalUnits =
    [ "hermes-agent" ]
    ++ lib.optional (config.services.hermes-dashboard.enable or false) "hermes-dashboard"
    ++ lib.optional (config.services.hermes-nicegui.enable or false) "hermes-nicegui";

  # The ACTIVE generation — what is running right now — not the highest-
  # numbered one. `nix-env --rollback` does NOT create a new generation: it
  # only moves the `(current)` marker back while the highest number stays put.
  # So `list-generations | tail -n1` keeps returning the stale high number
  # after a rollback and the watchdog would roll back PAST last-known-good
  # forever. Reading the system profile link is the only reliable answer.
  # NB: the link is `system-<N>-link` (no leading dash) — a pattern expecting
  # `-system-` silently never matches and returns empty, which breaks the
  # watchdog's rollback guard AND writes an empty last-known-good.
  #
  # NB for anyone embedding this inside a nested `bash -c '...'` (single-
  # quoted): the sed expression below contains single quotes and will
  # prematurely close that outer string. Use `readlink ... | tr -dc 0-9`
  # there instead (see watchdogRollback).
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

  # set -e is REQUIRED: cfg.healthCheck's default ends with `hermes doctor
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

  # Pre-switch memory gate. Included verbatim by the deploy script before any
  # side effect, and standalone by its --check-memory-gate dry-run mode.
  # Headroom = MemAvailable + SwapFree from /proc/meminfo; below
  # minAvailableMemMb a rebuild risks wedging the box (an uncapped rebuild
  # once did: load 9.37, two build workers died, host bounced), so we defer
  # instead of gambling it. Exit code 42 = gated. FORCE_GATE/CHECK_ONLY are
  # set by the deploy script's argument parser above the inclusion point.
  memoryGate = ''
    AV=$(awk '/^MemAvailable:/ { printf "%d", $2/1024; f=1 } END { if (!f) print 0 }' /proc/meminfo)
    SW=$(awk '/^SwapFree:/    { printf "%d", $2/1024 }' /proc/meminfo)
    HEADROOM=$((AV + SW))
    FLOOR=${toString cfg.minAvailableMemMb}
    if [ "$HEADROOM" -lt "$FLOOR" ]; then
      MSG="memory gate: ''${HEADROOM}MB headroom ($AV MB avail + $SW MB swapfree) below required $FLOOR MB"
      if [ "$FORCE_GATE" = "true" ]; then
        echo "WARNING: $MSG — proceeding (--force-memory-gate)"
      else
        echo "ABORT: $MSG"
        echo "The box is under memory pressure; rebuilding now risks wedging it."
        echo "Retry when quiet, or bypass once for genuine emergencies:"
        echo "  hermes-deploy --force-memory-gate"
        exit 42
      fi
    else
      echo "memory gate: ''${HEADROOM}MB headroom >= $FLOOR MB floor - OK"
    fi
  '';

  # Bash functions shared by every script/transient below. Each caller
  # defines its own `log()` first; these assume it exists.
  #
  # tag_gen: record which commit produced a generation, right after a switch
  # (regardless of whether it turns out healthy — this is a factual mapping,
  # not a health judgement; last-known-good tracks health separately).
  #
  # sync_repo_to_gen: the git-ledger half of a rollback. A Nix-level rollback
  # only restores the system closure; it does nothing to ${cfg.repoDir}'s
  # working tree. Without also moving the outer repo (and therefore every
  # vendor/* submodule gitlink in it) back to the commit that produced the
  # target generation, the working tree stays at the newer/bad commit and the
  # next deploy's `git add -A` would just re-propose the exact code that was
  # just rolled back from. `git submodule update` after the reset moves each
  # vendor/* submodule to the commit recorded in that commit's tree (a plain,
  # well-defined git operation — submodules are ordinary gitlinks).
  #
  # No single quotes in here: this gets embedded verbatim inside nested
  # `bash -c '...'` blocks (see watchdogRollback) too.
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

  # Roll back exactly one generation WITHOUT nixos-rebuild. nixos-rebuild's
  # `--rollback` self-locates by building itself from <nixpkgs/nixos>, which
  # needs NIX_PATH (nixpkgs + nixos-config) — a flake-only box doesn't have
  # those, so it fails with "file 'nixos-config' was not found". The
  # low-level equivalent is identical in effect: `nix-env --rollback` moves
  # the system profile marker back one generation (it does NOT create a new
  # generation), then we re-run that generation's own switch-to-configuration.
  #
  # switch-to-configuration runs as a DETACHED transient unit: it treats any
  # active-but-unwanted unit (wantedBy=[]) as "to stop", and
  # hermes-rollback/hermes-deploy/hermes-watchdog are exactly that while they
  # run the switch — a foreground switch gets TERM'd mid-flight. As a
  # transient the switch survives that stop, because runtime-registered
  # transients belong to no generation diff.
  #
  # The transient is NAMED and memory-CAPPED. The name doubles as a mutex (a
  # second rollback can't interleave switches with this one, or with a
  # concurrent deploy's switch — see deployScript) and MemoryMax bounds
  # rollback-time memory like the deploy path. `--wait` is what makes
  # systemd-run BLOCK until the switch has TERMINATED: without it, the start
  # job of a Type=simple transient completes at fork and systemd-run returns
  # in milliseconds, before the switch has actually applied.
  rollbackGeneration = ''
    # `|| true`: on a single-generation system there is nothing older to roll
    # back to and this fails ("no profile version older than the current
    # exists") — that must not abort the caller under set -e before the
    # switch-to-configuration/git-sync steps below, which still need to run
    # (reflecting back "we're still on the same generation", correctly a
    # no-op) rather than leaving the script dead mid-rollback.
    nix-env -p /nix/var/nix/profiles/system --rollback || true
    systemd-run --unit=hermes-rollback-switch --collect --wait --quiet \
      -p MemoryMax=${cfg.switchMemoryMax} \
      bash -c '
        ${pathExport}
        /nix/var/nix/profiles/system/bin/switch-to-configuration switch || true
        systemctl start ${lib.concatStringsSep " " criticalUnits} 2>/dev/null || true
      '
  '';

  # Watchdog variant of rollbackGeneration: rolls back exactly one generation
  # AND confirms/blesses the result AND syncs the git ledger, all INSIDE the
  # detached transient. hermes-watchdog.service runs in the foreground (see
  # below), so its rollback's own switch stopping hermes-watchdog.service
  # (its unit file changed) would TERM this script mid-rollback — anything
  # that must survive that has to live inside the transient, not after the
  # systemd-run line in the outer script. `tr -dc 0-9` avoids the sed
  # single-quote clash inside this nested bash -c (same extraction result as
  # currentGeneration, for a system-<N>-link).
  watchdogRollback = ''
    # See rollbackGeneration above: `|| true` because a single-generation
    # system has nothing older to roll back to, and that must not abort this
    # transient before the git-sync/bless steps below.
    nix-env -p /nix/var/nix/profiles/system --rollback || true
    systemd-run --unit=hermes-watchdog-switch --collect --wait --quiet \
      -p MemoryMax=${cfg.switchMemoryMax} \
      bash -c '
        ${pathExport}
        log() { echo "[hermes-watchdog] $*"; }
        ${gitSyncFns}
        /nix/var/nix/profiles/system/bin/switch-to-configuration switch || true
        systemctl start ${lib.concatStringsSep " " criticalUnits} 2>/dev/null || true
        ROLLED_TO=$(readlink /nix/var/nix/profiles/system | tr -dc 0-9)
        sync_repo_to_gen "$ROLLED_TO"
        sleep 10
        if timeout ${toString cfg.gracePeriod} bash ${healthCheckScript}; then
          echo "$ROLLED_TO" > ${cfg.stateDir}/last-known-good
          echo "[hermes-watchdog] rollback confirmed healthy — last-known-good updated to generation $ROLLED_TO" >> /var/log/hermes-deploy.log
        else
          echo "[hermes-watchdog] rollback landed but health check still failing — last-known-good left unchanged" >> /var/log/hermes-deploy.log
        fi
      '
  '';

  deployScript = pkgs.writeShellScript "hermes-deploy-script" ''
    set -euo pipefail
    ${pathExport}

    log() { echo "[hermes-deploy] $*"; }
    ${gitSyncFns}

    # ── Argument / env handling ──────────────────────────────────────────
    #   --force-memory-gate  override the pre-switch memory gate this run
    #   --check-memory-gate  evaluate ONLY the gate and exit (0 green /
    #                        42 gated): the dry-run behind
    #                        hermes-deploy-check.service, so operators and
    #                        tests can prove the gate evaluates without
    #                        deploying anything.
    # The bypass crosses the systemctl boundary as a consumed FLAG FILE, not
    # client environment: `systemctl start` does not forward the caller's env
    # into the started unit, but a file under ${cfg.stateDir} is visible to
    # the root-run script regardless of how it was launched. It is consumed
    # (removed) atomically by the next deploy run — one bypass, one deploy —
    # and cannot leak to unrelated units the way manager-scoped environment
    # could.
    FORCE_GATE=false
    CHECK_ONLY=false
    for a in "$@"; do
      case "$a" in
        --force-memory-gate) FORCE_GATE=true ;;
        --check-memory-gate) CHECK_ONLY=true ;;
      esac
    done
    if [ -f ${cfg.stateDir}/.force-memory-gate-once ]; then
      rm -f ${cfg.stateDir}/.force-memory-gate-once
      FORCE_GATE=true
    fi

    # Dry-run mode: run ONLY the memory gate, zero side effects. Must sit
    # BEFORE the git phases so a check never touches the ledger commit.
    if [ "$CHECK_ONLY" = "true" ]; then
      ${memoryGate}
      # The gate exits 42 itself when gated; reaching this line = green.
      echo "memory gate check: PASS"
      exit 0
    fi

    # Pre-switch memory gate: a gated deploy touches NOTHING — the gate runs
    # before the git phases, so a refused deploy leaves no ledger commit to
    # rewind and no half-synced state behind.
    ${memoryGate}

    # No git remote: the checkout in ${cfg.repoDir} IS the source of truth.
    # The operator pushes a fresh copy (scripts/deploy.sh rsyncs it), and the
    # bot commits directly here then rebuilds. Record any uncommitted edits in
    # the local ledger so the deploy is always a committed state.
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

    log "building & switching generation (detached, capped, serial, waited)"
    # The switch runs as a NAMED, memory-CAPPED transient that systemd-run
    # WAITS FOR:
    #   i.   The unit name is the MUTEX: systemd refuses to create a second
    #        hermes-deploy-switch while one exists, so two concurrent
    #        deploys collide HERE (loudly) instead of interleaving two
    #        switches on the same box.
    #   ii.  MemoryMax bounds even a runaway builder.
    #   iii. --max-jobs/--cores keep the build serial (one derivation at a
    #        time) so a closure-touching change can never fork enough
    #        builders to starve the box.
    #   iv.  --wait blocks until the transient has TERMINATED and propagates
    #        its real exit status (the inner bash re-exits
    #        ''${PIPESTATUS[0]} after the `| tee`, so nixos-rebuild's REAL
    #        status propagates instead of tee's). A non-zero systemd-run exit
    #        is classified by a UNIT-STATE PROBE, not by matching systemd's
    #        stderr wording (which varies across versions): if
    #        hermes-deploy-switch is still active/activating under our fixed
    #        name, another deploy's switch owns the mutex and we exit 1 right
    #        here; anything else means OUR switch died mid-flight (e.g. the
    #        Rust switch-to-configuration-ng panicking with exit 101 when a
    #        stopped unit's process tree owned its stdout pipe, upstream
    #        nixpkgs#462179, or a MemoryMax OOM-kill of the build) and
    #        SWITCH_FAILED falls through to the post-switch safety net +
    #        health check + auto-rollback machinery below, which heal
    #        exactly like a clean failure would. Accepted race: a mutex hit
    #        whose unit TERMINATES between the refusal and the probe lands in
    #        the heal branch — safe, because the net is idempotent and the
    #        health check arbitrates the result.
    SWITCH_FAILED=""
    RC=0
    systemd-run --unit=hermes-deploy-switch --collect --wait --quiet \
         -p MemoryMax=${cfg.switchMemoryMax} \
         bash -c '
           ${pathExport}
           nixos-rebuild switch --flake "${cfg.repoDir}#${cfg.flakeAttr}" \
             --max-jobs ${toString cfg.maxJobs} --cores ${toString cfg.cores} \
             2>&1 | tee -a /var/log/hermes-deploy.log
           exit ''${PIPESTATUS[0]}
         ' || RC=$?
    if [ "$RC" -ne 0 ]; then
      SW_STATE=$(systemctl show -p ActiveState --value hermes-deploy-switch 2>/dev/null || echo gone)
      case "$SW_STATE" in
        active|activating)
          log "could not start switch transient hermes-deploy-switch (another deploy in flight)"
          exit 1
          ;;
        *)
          log "switch transient FAILED (exit $RC, unit state: $SW_STATE) - running post-switch safety net + health check anyway"
          SWITCH_FAILED=1
          ;;
      esac
    fi

    # Post-switch safety net (idempotent): the switch may have stopped any of
    # the critical units to replace their unit files — including this
    # deploy's own unit, which is a no-op because we run detached. Bring
    # every critical unit back before health-checking (`systemctl start` is a
    # no-op on active units) and clear any stale failed state on this
    # deploy unit.
    systemctl start ${lib.concatStringsSep " " criticalUnits} 2>/dev/null || true
    systemctl reset-failed hermes-deploy 2>/dev/null || true

    CUR_GEN=$(${currentGeneration})
    tag_gen "$CUR_GEN"

    log "asking agent for a health report (grace ${toString cfg.gracePeriod}s)"
    if timeout ${toString cfg.gracePeriod} bash ${healthCheckScript}; then
      # A failed switch that nonetheless leaves the box healthy is NOT a
      # successful deploy: the generation is unchanged or only partially
      # applied while the tree is committed. Never bless that with
      # "deploy OK" — surface the failure so someone looks at it.
      if [ "$SWITCH_FAILED" = "1" ]; then
        echo "deploy FAILED (generation $CUR_GEN): switch failed but system healthy — generation unchanged or partially applied; verify manually"
        exit 1
      fi
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
    ${rollbackGeneration} 2>&1 | tee -a /var/log/hermes-deploy.log || true
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
    set -euo pipefail
    ${pathExport}
    log() { echo "[hermes-rollback] $*"; }
    ${gitSyncFns}

    log "rolling back system by one generation"
    # Failure observability: the launcher (hermes-rollback.service) drops
    # --collect, so a FAILED hermes-rollback-run transient is kept for
    # inspection instead of being garbage-collected. Clear a stale failed
    # predecessor before launching this run's switch so failure bookkeeping
    # starts clean.
    systemctl reset-failed hermes-rollback-run 2>/dev/null || true
    # `|| true`: a single-generation system (nowhere to roll back to) makes
    # `nix-env --rollback` fail — that must not abort the script before the
    # git-sync step below, which is what actually needs to run regardless
    # (it just reflects back whatever generation we're really on).
    ${rollbackGeneration} 2>&1 | tee -a /var/log/hermes-deploy.log || true

    # Move the outer repo (and every vendor/* submodule) back to match
    # whichever generation we actually landed on.
    ROLLED_TO=$(${currentGeneration})
    sync_repo_to_gen "$ROLLED_TO"

    # Mark this generation known-good only if the agent reports healthy again.
    sleep 10
    if timeout ${toString cfg.gracePeriod} bash ${healthCheckScript}; then
      echo "$ROLLED_TO" > ${cfg.stateDir}/last-known-good
    fi
  '';

  watchdogScript = pkgs.writeShellScript "hermes-watchdog-script" ''
    set -euo pipefail
    ${pathExport}
    log() { echo "[hermes-watchdog] $*"; }
    ${gitSyncFns}

    KNOWN_GOOD="${cfg.stateDir}/last-known-good"
    mkdir -p ${cfg.stateDir}

    CRITICAL_UNITS="${lib.concatStringsSep " " criticalUnits}"

    bless() {
      CUR_GEN=$(${currentGeneration})
      if [ ! -f "$KNOWN_GOOD" ] || [ "$(cat "$KNOWN_GOOD")" != "$CUR_GEN" ]; then
        echo "$CUR_GEN" > "$KNOWN_GOOD"
        log "healthy — updated last-known-good to generation $CUR_GEN"
      fi
    }

    if timeout ${toString cfg.gracePeriod} bash ${healthCheckScript}; then
      bless
      exit 0
    fi

    log "unhealthy: health check failed"

    # Content-lane first: try reverting skills/tools/MCP registrations before
    # spending a generation. Cheap, no rebuild; content is the likely culprit
    # for a fast-path change that broke something non-systemic.
    ${lib.optionalString (cfg.contentRecovery != "") ''
      log "unhealthy — trying content recovery first"
      if timeout ${toString cfg.gracePeriod} bash ${contentRecoveryScript}; then
        sleep 10
        if timeout ${toString cfg.gracePeriod} bash ${healthCheckScript}; then
          bless
          log "content recovery restored health"
          exit 0
        fi
      fi
    ''}

    if [ ! -f "$KNOWN_GOOD" ]; then
      log "no last-known-good recorded; not auto-rolling back"
      exit 0
    fi

    # Classify critical units. "activating" = systemd is still starting it (a
    # deploy/restart in flight) — wait for it to settle instead of condemning
    # the generation. Anything else non-active is down.
    ACTIVATING=""
    DOWN=""
    for u in $CRITICAL_UNITS; do
      case "$(systemctl show -p ActiveState --value "$u")" in
        active) ;;
        activating) ACTIVATING="$ACTIVATING $u" ;;
        *) DOWN="$DOWN $u" ;;
      esac
    done

    if [ -n "$ACTIVATING" ]; then
      log "critical unit(s) still starting:$ACTIVATING — waiting up to ${toString cfg.gracePeriod}s for health"
      # A restart in flight (e.g. the gateway rebooting after a deploy) must
      # not be rolled back on a fluke tick: poll the health check until the
      # units settle or the grace period runs out.
      if timeout ${toString cfg.gracePeriod} bash -c 'until bash ${healthCheckScript}; do sleep 10; done' 2>/dev/null; then
        bless
        exit 0
      fi
      log "critical unit(s) still not healthy after grace period — treating as down"
      DOWN="$DOWN$ACTIVATING"
    fi

    CUR_GEN=$(${currentGeneration})
    LAST_GOOD=$(cat "$KNOWN_GOOD")

    # A dead critical unit (dashboard/nicegui/agent) must never be blessed and
    # IS rollback-worthy even when it is not newer than last-known-good — a
    # generation can be "not newer" and still have broken a unit that was
    # only just enabled. watchdogRollback blesses + syncs the git ledger from
    # INSIDE its own detached transient (see above) because rolling back can
    # stop hermes-watchdog.service itself mid-script.
    if [ -n "$DOWN" ]; then
      log "critical unit(s) down:$DOWN — generation $CUR_GEN is broken"
      log "rolling back exactly one generation"
      ${watchdogRollback} 2>&1 | tee -a /var/log/hermes-deploy.log || true
      exit 1
    fi

    if [ "$CUR_GEN" -gt "$LAST_GOOD" ] 2>/dev/null; then
      log "current generation $CUR_GEN is newer than last-known-good $LAST_GOOD"
      log "rolling back exactly one generation"
      ${rollbackGeneration} 2>&1 | tee -a /var/log/hermes-deploy.log || true
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
        # Ask the agent itself: every critical unit must be active — the
        # gateway plus (when enabled) the web dashboard and hermes-nicegui —
        # AND `hermes doctor` (the agent's own self-diagnostic) must pass. A
        # dead nicegui/dashboard must never be blessed. Derived from the
        # agent's own options so it stays coherent if stateDir/user change.
        for u in ${lib.concatStringsSep " " criticalUnits}; do
          systemctl is-active --quiet "$u" || exit 1
        done
        runuser -u ${config.services.hermes-agent.user} -- env HERMES_HOME=${config.services.hermes-agent.stateDir}/.hermes \
          hermes doctor >/dev/null 2>&1
      '';
      description = "Bash command; exit 0 = healthy. Runs as root and should ask Hermes itself. The healthCheckUrls probes are appended to this script whenever it runs.";
    };

    healthCheckUrls = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = ''
        Public http(s) URLs the deploy+watchdog health check probes; PASS =
        any HTTP response < 500 (2xx/3xx/401/403/404 all prove the tunnel
        connector + origin are alive); HTTP >= 500 (530/502/521/522) or
        network failure/timeout = FAIL. Empty list = probes skipped. Set this
        to your public hostname(s) (e.g. via the Cloudflare tunnel) — a
        generation that breaks the tunnel but leaves the local gateway
        healthy is still a broken deploy.
      '';
    };

    watchdogInterval = lib.mkOption {
      type = lib.types.str;
      default = "15min";
      description = "How often the watchdog re-checks gateway health.";
    };

    # ── Resource hardening ────────────────────────────────────────────
    # Two incident vectors this guards against: (a) deploys launched from
    # inside the gateway tree self-killed mid-switch; (b) a full uncapped
    # rebuild wedged a 7.8G-RAM box (load 9.37, two build workers died, host
    # bounced). The options below gate the switch path on real memory
    # headroom and cap every switch/build transient.

    minAvailableMemMb = lib.mkOption {
      type = lib.types.int;
      default = 2048;
      description = ''
        Pre-switch memory gate floor in MB. Headroom = MemAvailable +
        SwapFree. A --max-jobs 1 rebuild + evaluation needs roughly 1-2GB
        headroom; the default gives some margin beside whatever else is
        resident. SwapFree counts as headroom because the kernel reclaims
        cache and swaps before OOM — conservative given thrash.
      '';
    };

    maxJobs = lib.mkOption {
      type = lib.types.int;
      default = 1;
      description = ''
        nix build --max-jobs on the deploy switch path. One derivation at a
        time so a closure-touching change can never fork enough builders to
        starve the box.
      '';
    };

    cores = lib.mkOption {
      type = lib.types.int;
      default = 2;
      description = "nix build --cores on the deploy switch path: per-builder make -j cap.";
    };

    switchMemoryMax = lib.mkOption {
      type = lib.types.str;
      default = "3G";
      description = ''
        Systemd MemoryMax for every switch/build transient (deploy, rollback,
        watchdog). Small enough that even a runaway builder cannot evict the
        resident stack faster than the kernel can reclaim.
      '';
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
      find ${cfg.repoDir} -mindepth 1 \
        -path ${cfg.repoDir}/state -prune -o \
        -exec chown ${config.services.hermes-agent.user}:${config.services.hermes-agent.group} {} \;
      if [ ! -d ${cfg.repoDir}/.git ]; then
        echo "hermes-deploy: initialising git repo in ${cfg.repoDir}"
        ${lib.getExe pkgs.git} -C ${cfg.repoDir} init -q
        ${lib.getExe pkgs.git} -C ${cfg.repoDir} config user.name "hermes-deploy"
        ${lib.getExe pkgs.git} -C ${cfg.repoDir} config user.email "hermes-deploy@localhost"
        ${lib.getExe pkgs.git} -C ${cfg.repoDir} add -A 2>/dev/null || true
        ${lib.getExe pkgs.git} -C ${cfg.repoDir} commit -q -m "initial import from operator deploy" 2>/dev/null || true
      fi
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
        # Self-kill hardening: a switch that replaces hermes unit files stops
        # hermes-deploy.service itself, and everything left in its cgroup is
        # SIGTERM'd mid-switch — aborting the switch between its stop and
        # start phases. Run the whole deploy in a detached transient unit
        # instead: this launcher exits before the switch runs, so stopping
        # this unit is a no-op and the deploy (switch + safety net + health
        # check + rollback) runs to completion detached. The transient is
        # also NAMED (hermes-deploy-run) — while it runs the fixed name is a
        # loud mutex (a second launch collides instead of piling up anonymous
        # run-<hex> units racing each other). This launcher KEEPS --collect,
        # so a FAILED run transient is garbage-collected rather than left
        # visible; the durable guarantees are the name mutex while running +
        # the journald entries, not a lingering failed unit.
        ExecStart = "systemd-run --unit=hermes-deploy-run --collect --no-block ${deployScript}";
        TimeoutStartSec = 0;
      };
    };

    # Dry-run target for `hermes-deploy --check-memory-gate`. Runs ONLY the
    # deploy script's memory gate (its --check-memory-gate mode exits before
    # any git/deploy side effect), so operators and tests can prove the gate
    # evaluates without touching the system. wantedBy = []: never starts on
    # its own; exit 42 surfaces as a failed unit so `systemctl start` reports
    # it synchronously.
    systemd.services.hermes-deploy-check = {
      description = "Hermes deploy memory-gate self-check (no deploy)";
      wantedBy = [ ];
      path = [ config.system.path ];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${deployScript} --check-memory-gate";
      };
    };

    systemd.services.hermes-rollback = {
      description = "Roll back Hermes to the previous git/system generation";
      wantedBy = [ ];
      path = [ config.system.path ];
      serviceConfig = {
        Type = "oneshot";
        # Detached like the deploy unit — same class of fix: the rollback's
        # own switch stops hermes-rollback.service itself (its unit file
        # changed across the rollback), which would otherwise TERM the
        # script mid-flight. Detaching the WHOLE rollback also lets its
        # post-switch blessing (sync_repo_to_gen + sleep + health check +
        # last-known-good write) run to completion instead of dying halfway
        # through. Named transient (hermes-rollback-run). UNLIKE the deploy
        # launcher this one drops --collect on purpose: a FAILED transient is
        # kept instead of GC'd, so a failed rollback stays inspectable (the
        # script clears a stale failed predecessor via reset-failed before
        # relaunching). Callers that need the RESULT must wait for the
        # transient and read `systemctl is-failed` (the hermes-rollback CLI
        # wrapper and scripts/rollback.sh do).
        ExecStart = "systemd-run --unit=hermes-rollback-run --no-block ${rollbackScript}";
        TimeoutStartSec = 0;
      };
    };

    systemd.services.hermes-watchdog = {
      description = "Auto-rollback watchdog for the Hermes gateway";
      # Order after the gateway, but do NOT require it: the watchdog exists
      # precisely to roll back when the gateway (or any critical unit) is
      # down — binding its start to the gateway's would disable it exactly
      # when it is needed.
      after = [ "hermes-agent.service" ];
      path = [ config.system.path ];
      serviceConfig = {
        Type = "oneshot";
        # Foreground ON PURPOSE (unlike deploy/rollback): timer-driven
        # oneshot whose own rollback path is already internally detached
        # inside the hermes-watchdog-switch transient (see watchdogRollback),
        # so nothing here needs to survive a switch stop — and the timer
        # wants synchronous completion anyway.
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
          # Emergency bypass + dry-run for the pre-switch memory gate
          # (services.hermes-deploy.minAvailableMemMb).
          if [ "''${1:-}" = "--force-memory-gate" ]; then
            # systemctl does not forward client env to the started unit, so
            # the bypass crosses the boundary as a consumed flag file: the
            # next deploy run removes it and forces the gate open exactly
            # once. No manager-scoped environment is touched, so nothing can
            # leak into unrelated units (watchdog, rollback, ...).
            : > ${cfg.stateDir}/.force-memory-gate-once 2>/dev/null || (mkdir -p ${cfg.stateDir} && : > ${cfg.stateDir}/.force-memory-gate-once)
            rc=0
            systemctl start hermes-deploy.service || rc=$?
            exit $rc
          fi
          if [ "''${1:-}" = "--check-memory-gate" ]; then
            # Evaluate the gate with zero side effects (no deploy). The
            # oneshot runs synchronously: exit 0 = gate green, failure
            # (exit-code 42) = gated.
            exec systemctl start hermes-deploy-check.service
          fi
          exec systemctl start hermes-deploy.service
        '')
        (pkgs.writeShellScriptBin "hermes-rollback" ''
          # hermes-rollback.service launches a DETACHED transient
          # (hermes-rollback-run) and returns immediately, so waiting for it
          # here mirrors scripts/rollback.sh's poll loop — otherwise an
          # operator races a still-running rollback. ~8 min max. The
          # launcher deliberately DROPS --collect: a FAILED transient stays
          # inspectable instead of being garbage-collected, and
          # `systemctl is-failed` on it is the authoritative verdict (a
          # successful oneshot transient reports not-failed once gone).
          systemctl start hermes-rollback.service || exit $?
          for i in $(seq 1 240); do
            st=$(systemctl is-active hermes-rollback-run.service 2>/dev/null) || true
            case "$st" in active|activating) sleep 2 ;; *) break ;; esac
          done
          if systemctl is-failed hermes-rollback-run.service >/dev/null 2>&1; then
            echo "hermes-rollback: rollback transient FAILED (see journalctl -u hermes-rollback-run)" >&2
            exit 1
          fi
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
