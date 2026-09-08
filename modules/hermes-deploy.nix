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
# Vendored source (hermes-agent, hermes-nicegui, xaelWiki) ships as rev-pinned
# GitHub flake inputs. Every gen-<N> tag captures the flake.lock at build time,
# so a rollback to that tag restores the correct input revs automatically. No
# submodule sync — flake.lock resets to the target tag's content and Nix
# re-freshes the input tarballs on next build.

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
    if hasUserAll then
      userChecks
    else
      let
        withHindsight = _defaults // userChecks;
        withoutHindsight = (builtins.removeAttrs _defaults [ "hindsight" ]) // userChecks;
      in
      if hindsightEnabled then withHindsight else withoutHindsight;

  # Store path for each check script
  healthScripts = lib.mapAttrs (
    name: check: pkgs.writeShellScript "hermes-health-${name}" check.check
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
      ''
      + cfg.contentRecovery
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

  # ── Session-id callback (self-deploy feedback) ──────────────────────
  # When a session self-deploys it hands its own session id to the deploy
  # (`hermes-deploy --callback <id>`); the deployScript annexes the verdict
  # into the same marker file and self-posts it back into the session via the
  # in-pod api_server (/v1/chat/completions + X-Hermes-Session-Id) once the
  # (possibly restarted) gateway is healthy. A startup hook is the backstop
  # for the restart case, where the detached deployScript may still be running
  # when the new gateway boots.
  hermesHome = "${config.services.hermes-agent.stateDir}/.hermes";
  callbackFile = "${hermesHome}/.deploy_callback.json";

  # Self-post the deploy verdict into the originating session. Runs from the
  # detached deployScript AFTER the switch, so it survives the gateway restart.
  # Reads the api_server credentials from the default profile's .env (agenix-
  # decoded at activation); waits for the listener to come up (the gateway may
  # have just restarted) before POSTing. Marks the marker delivered on success
  # so the startup backstop hook does not re-deliver. Non-fatal: any failure
  # leaves the marker for the hook.
  deliverCallbackScript = pkgs.writeShellScript "hermes-deploy-callback" ''
        set -uo pipefail
        CALLBACK_FILE="$1"
        EXIT_CODE="$2"
        STATUS_LABEL="$3"
        GENERATION="$4"
        PY3="${pkgs.python3}/bin/python3"

        [ -f "$CALLBACK_FILE" ] || exit 0
        SESSION_ID=$($PY3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("session_id",""))' "$CALLBACK_FILE" 2>/dev/null || true)
        [ -n "$SESSION_ID" ] || exit 0

        ENV_FILE="${hermesHome}/.env"
        API_KEY=$(sed -n 's/^API_SERVER_KEY=//p' "$ENV_FILE" 2>/dev/null | head -1)
        API_PORT=$(sed -n 's/^API_SERVER_PORT=//p' "$ENV_FILE" 2>/dev/null | head -1)
        API_HOST=$(sed -n 's/^API_SERVER_HOST=//p' "$ENV_FILE" 2>/dev/null | head -1)
        API_PORT="''${API_PORT:-8443}"
        API_HOST="''${API_HOST:-127.0.0.1}"
        [ -n "$API_KEY" ] || { echo "no API_SERVER_KEY - cannot deliver callback"; exit 0; }

        if [ "$EXIT_CODE" = "0" ]; then
          MSG="✅ Self-deploy finished successfully (exit 0, generation $GENERATION)."
        else
          MSG="❌ Self-deploy failed - rolled back (exit $EXIT_CODE, generation $GENERATION)."
        fi

        # Wait for the api_server listener (the gateway may have just restarted).
        deadline=$((SECONDS + 90))
        until $PY3 -c "import socket,sys; s=socket.create_connection((sys.argv[1], int(sys.argv[2])), 1); s.close()" "$API_HOST" "$API_PORT" 2>/dev/null; do
          if (( SECONDS >= deadline )); then
            echo "api_server not reachable - leaving callback marker for startup hook"
            exit 0
          fi
          sleep 2
        done

        $PY3 - "$API_HOST" "$API_PORT" "$API_KEY" "$SESSION_ID" "$MSG" <<'PYEOF'
    import json, sys, urllib.request
    host, port, key, sid, msg = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
    url = f"http://{host}:{port}/v1/chat/completions"
    payload = json.dumps({"model": "hermes-agent", "messages": [{"role": "user", "content": msg}], "stream": False}).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Authorization": f"Bearer {key}",
        "X-Hermes-Session-Id": sid,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            resp.read()
        print("deploy callback delivered to session", sid)
    except Exception as e:
        print("deploy callback delivery failed:", e, file=sys.stderr)
        sys.exit(0)
    PYEOF
        # Mark delivered so a later startup hook does not re-deliver.
        $PY3 - "$CALLBACK_FILE" "$EXIT_CODE" "$STATUS_LABEL" "$GENERATION" <<'PYEOF'
    import json, sys
    path, code, status, gen = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    try:
        d = json.load(open(path))
    except Exception:
        d = {}
    d["exit_code"] = int(code)
    d["status"] = status
    d["generation"] = gen
    d["delivered"] = True
    d["delivered_at"] = __import__("datetime").datetime.now().isoformat()
    json.dump(d, open(path, "w"))
    PYEOF
  '';

  # Bash function the deployScript calls at every terminal verdict.
  deliverFn = ''
    deliver_callback() {
      local exit_code="$1" status_label="$2" generation="$3"
      [ -f ${callbackFile} ] || return 0
      ${deliverCallbackScript} ${callbackFile} "$exit_code" "$status_label" "$generation"
    }
  '';

  # ── Startup-hook backstop for the self-deploy callback ─────────────
  # The detached deployScript is the PRIMARY delivery path (it self-posts the
  # verdict into the session once the gateway is healthy). This hook catches
  # the restart race: the deployScript survives the switch and may still be
  # running when the new gateway boots, so the hook schedules a background
  # watcher that delivers as soon as a verdict appears (and is not yet
  # delivered). Non-blocking; an error never affects startup.
  deployCallbackHookYaml = pkgs.writeText "deploy-callback-HOOK.yaml" ''
    name: deploy-callback
    description: Deliver the self-deploy verdict back into the originating session after a gateway restart.
    events:
      - gateway:startup
  '';
  deployCallbackHookHandler = pkgs.writeText "deploy-callback-handler.py" ''
    """gateway:startup backstop for the self-deploy callback.

    The detached deployScript is the primary delivery path; it self-posts the
    deploy verdict into the originating session once the (possibly restarted)
    gateway is healthy. This hook catches the case where the deployScript's
    delivery raced the restart: the deployScript survives the switch and may
    still be running when the new gateway boots, so this hook schedules a
    background watcher that delivers as soon as a verdict appears (and is not
    yet delivered). Non-blocking; errors never affect startup.
    """

    import asyncio
    import json
    import os
    import time
    from pathlib import Path

    _WATCH_SECONDS = 600
    _POLL_SECONDS = 5


    def _marker_path() -> Path:
        return Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))) / ".deploy_callback.json"


    async def _deliver(path: Path, d: dict) -> None:
        import aiohttp

        sid = d.get("session_id")
        if not sid:
            return
        key = os.environ.get("API_SERVER_KEY", "")
        host = os.environ.get("API_SERVER_HOST", "127.0.0.1")
        port = os.environ.get("API_SERVER_PORT", "8443")
        if not key:
            return
        exit_code = d.get("exit_code")
        gen = d.get("generation", "")
        if exit_code == 0:
            msg = f"✅ Self-deploy finished successfully (exit 0, generation {gen})."
        else:
            msg = f"❌ Self-deploy failed - rolled back (exit {exit_code}, generation {gen})."
        url = f"http://{host}:{port}/v1/chat/completions"
        payload = {"model": "hermes-agent", "messages": [{"role": "user", "content": msg}], "stream": False}
        headers = {"Authorization": f"Bearer {key}", "X-Hermes-Session-Id": sid}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as http:
            async with http.post(url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                await resp.read()
        d["delivered"] = True
        d["delivered_at"] = __import__("datetime").datetime.now().isoformat()
        json.dump(d, open(path, "w"))


    async def _watch_and_deliver(path: Path) -> None:
        start = time.monotonic()
        while time.monotonic() - start < _WATCH_SECONDS:
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                d = {}
            if d.get("session_id") and "exit_code" in d and not d.get("delivered"):
                try:
                    await _deliver(path, d)
                    return
                except Exception:
                    pass  # transient (api_server restarting) — retry
            await asyncio.sleep(_POLL_SECONDS)


    async def handle(event_type, context) -> None:
        path = _marker_path()
        if not path.exists():
            return
        asyncio.create_task(_watch_and_deliver(path))
  '';

  # ── Git sync functions ──────────────────────────────────────────────
  # Shared by deploy and rollback scripts.
  #
  # tag_gen: record which commit produced a generation, right after a switch
  # (regardless of whether it turns out healthy - this is a factual mapping,
  # not a health judgement; last-known-good tracks health separately).
  #
  # sync_repo_to_gen: the git-ledger half of a rollback. A Nix-level rollback
  # (rollbackGeneration above) only restores the system closure; it does
  # nothing to ${cfg.repoDir}'s working tree. Without also moving the outer
  # repo back to the commit that produced the target generation, the
  # working tree stays at the newer/bad commit and the next deploy's
  # `git add -A` would re-propose the broken config. A `git reset --hard` to
  # the gen-<N> tag also restores the flake.lock that pins the correct
  # vendored-source revs.
  gitSyncFns = ''
    tag_gen() {
      git -C ${cfg.repoDir} tag -f "gen-$1" HEAD >/dev/null 2>&1 || true
    }
    sync_repo_to_gen() {
      local gen="$1"
      if git -C ${cfg.repoDir} rev-parse -q --verify "refs/tags/gen-$gen" >/dev/null 2>&1; then
        git -C ${cfg.repoDir} reset --hard "gen-$gen"
        log "synced git ledger to gen-$gen"
      else
        log "FATAL: no gen-$gen tag - git ledger NOT moved, only Nix rolled back"
        log "Next deploy will re-package the newer source and re-propose broken config"
        log "This unit MUST exit 1 to prevent permanent Nix-source misalignment"
        exit 1
      fi
    }
  '';

  activationManifest = let
    strip = builtins.unsafeDiscardStringContext;
    names = builtins.sort builtins.lessThan (builtins.attrNames cfg.activationFiles);
    entries = map (key:
      let f = cfg.activationFiles.${key};
      in "${strip f.source}\t${strip f.dest}\t${strip f.mode}\t${strip f.owner}\t${strip f.group}"
    ) names;
  in pkgs.writeTextFile {
    name = "activation-manifest.tsv";
    text = (lib.concatStringsSep "\n" entries) + "\n";
  };

  # Activation script that syncs activation-managed files from the
  # Nix store onto the live system. It is `lib.stringAfter ["users"]` so
  # it runs AFTER ownership is established but BEFORE any agent starts.
  # The script reads the manifest, copies source→dest, then chmod+chown.
  # It is idempotent: a `cp --no-preserve` on every activation is safe.
  activationSyncScript = pkgs.writeShellScript "activation-sync" ''
    set -euo pipefail
    # Guard: skip if we are in rollback (state/ is root-owned, git reset
    # would delete our work; rollback must leave the PREVIOUS generation's
    # activations intact).
    if [ -n "${cfg.stateDir}" ] && [ -f "${cfg.stateDir}/rolling-back" ]; then
      echo "activation-sync: rollback in progress — skipping"
      exit 0
    fi

    while IFS=$'\t' read -r src dest mode owner group; do
      [ -z "$src" ] && continue
      if [ ! -e "$src" ]; then
        echo "activation-sync: WARNING source $src does not exist — skipping"
        continue
      fi
      mkdir -p "$(dirname "$dest")"
      cp --no-preserve=mode,owner,group "$src" "$dest"
      chmod "$mode" "$dest"
      chown "$owner:$group" "$dest"
      echo "activation-sync: synced $dest from $(basename $src)"
    done < ${activationManifest}
  '';

  # ── Deploy script ────────────────────────────────────────────────────
  # Build, switch, health-check, rollback if unhealthy.
  deployScript = pkgs.writeShellScript "hermes-deploy-script" ''
    set -euo pipefail

    # ── R3: deployment-time activation sync pass ───────────────────────
    # Copy activation-managed files BEFORE the nixos-rebuild switch so
    # the git ledger is consistent. If the switch fails and rolls back,
    # the sync pass runs again at next activation from the restored git.
    echo "[hermes-deploy] R3 activation-sync pass (pre-switch)"
    ${activationSyncScript}
    echo "[hermes-deploy] activation-sync pass done"

    log() { echo "[hermes-deploy] $*"; }
    ${gitSyncFns}
    ${deliverFn}

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
      git -C ${cfg.repoDir} add -A 2>/dev/null || true
      git -C ${cfg.repoDir} commit -q -m "deploy $(date -Is)" 2>/dev/null || true
    ) 200>/tmp/hermes-deploy-git.lock

    PREV_GEN=$(${currentGeneration})
    log "previous generation: $PREV_GEN"

    # Pre-deploy validation: fail fast on eval errors, vendor narHash drift,
    # or syntax mistakes instead of wasting time building and then failing.
    # This is the critical step — vendor edits that forget `nix flake lock`
    # will get caught here with a clear error message.
    log "pre-deploy validation (flake check --no-build)"
    if ! nix flake check --no-build "${cfg.repoDir}" 2>&1 | tee -a /var/log/hermes-deploy.log; then
      log "pre-deploy validation FAILED — aborting deploy"
      log "Run 'nix flake check --no-build' to see the full error."
      log "Common fix: commit/push to the fork reop, then `nix flake lock --update-input hermes-agent` (or the equivalent nicegui/xaelwiki input), commit flake.lock, and re-deploy."
      deliver_callback 1 "validation-failed" ""
      exit 1
    fi
    log "validation passed"

    # Deploy-time pre-checks (e.g. hermes-secrets check hook). Any failure
    # aborts BEFORE a new generation is built — the wrong moment to learn a
    # secret was dropped is after you just switched the system onto a state
    # that can't decrypt it.
    ${lib.concatMapStringsSep "\n" (pre: ''
      if ! ${pre} 2>&1 | tee -a /var/log/hermes-deploy.log; then
        log "deploy pre-check FAILED — aborting deploy"
        deliver_callback 1 "pre-check-failed" ""
        exit 1
      fi
      log "deploy pre-check passed: ${pre}"
    '') cfg.deployPreChecks}

    # ── Review gate ────────────────────────────────────────────────────
    ${lib.optionalString cfg.requireReview ''
      log "review gate enabled — checking for review approval"
      if [ -f "${cfg.repoDir}/.review-approved" ]; then
        log "review gate: .review-approved found — passing"
      else
        log "ERROR: review gate BLOCKED — no review approval"
        log "  The file .review-approved does not exist in the repo."
        log ""
        log "  To unblock, create and commit this file before deploying:"
        log "    touch .review-approved"
        log "    git add .review-approved"
        log "    git commit -m 'Approve for review deployment'"
        log "    hermes-deploy"
        log ""
        log "  This gate prevents unreviewed changes from being deployed."
        deliver_callback 1 "review-blocked" ""
        exit 1
      fi
    ''}

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
      deliver_callback 0 "success" "$CUR_GEN"
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
          deliver_callback 0 "content-recovery" "$CUR_GEN"
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
    deliver_callback 1 "rollback" "$ROLLED_TO"
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

    deployPreChecks = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = ''
        Commands run by the deploy script BETWEEN `nix flake check --no-build`
        and the switch, in order. Any non-zero exit aborts the deploy before
        a (possibly broken) generation is built. Used by the hermes-secrets
        module to gate on secret validity.
      '';
    };

    # ── Health check registry ──────────────────────────────────────────
    # Declared health checks; each gets a dedicated oneshot unit
    # (`hermes-health-<name>.service`) and is invoked during the deploy
    # gate. Host configs can add new checks (e.g. `ha-reachable`) just
    # by adding an entry. No timers - health is only polled at deploy
    # time.
    health.checks = lib.mkOption {
      type = lib.types.attrsOf (
        lib.types.submodule {
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
        }
      );
      default = { };
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

    # ── Review gate ────────────────────────────────────────────────────
    requireReview = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        When true, the deploy script requires a review marker before
        building and switching. The operator commits a file
        ``.review-approved`` to the repo (the deploy script checks that
        the file is tracked in git — a committed file persists across
        the auto-commit step, so it is always present when needed).
        A single commit of ``.review-approved`` enables review-gated
        deploys permanently. To disable, remove the file and commit.

        When false, deploys proceed without review (default for backward
        compatibility).
      '';
    };

    # ── R3 activation-drift trap (ticket t_012f76dd) ────────────────────
    # Files managed by activation-scripts need a git-side truth so that
    # activation can restore them when the Nix store path changes across a
    # rebuild (the file on disk is stale because the activation script writes
    # a NEW path but the file was not staged in the ledger).
    activationFiles = lib.mkOption {
      type = lib.types.attrsOf (
        lib.types.submodule {
          options = {
            source = lib.mkOption {
              type = lib.types.path;
              description = "Source file in the repo (the git-side truth).";
            };
            dest = lib.mkOption {
              type = lib.types.str;
              description = "Absolute path where the file lives in the NixOS system.";
            };
            mode = lib.mkOption {
              type = lib.types.str;
              default = "0644";
              description = "Octal file mode for the destination.";
            };
            owner = lib.mkOption {
              type = lib.types.str;
              description = "Owner for chown.";
            };
            group = lib.mkOption {
              type = lib.types.str;
              description = "Group for chown.";
            };
          };
        }
      );
      default = {};
      description = ''
        Tab-separated manifest: source (repo-relative or absolute), dest
        (absolute), mode, owner, group. Evaluated at Nix-build time so the
        activation script can do `cp --no-preserve=mode,owner,group` and
        then `chmod/chown` — the build-time source path is in the Nix store,
        the dest is in the running system.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    # ── Git ownership: the worktree is hermes-owned (the bot must edit it)
    #    but git is ALSO run by root (deploy/rollback services,
    #    hermes-status, and every activation). git >=2.35 refuses to operate on
    #    a repo owned by another user (CVE-2022-24765), so without an explicit
    #    exception every root-run git command fails with "detected dubious
    #    ownership". Scope the exception to exactly the repos this module
    #    manages — never `safe.directory = *`, which would let the sandboxed
    #    bot trick root into running git hooks.
    environment.etc."gitconfig" = {
      text = ''
        [safe]
          directory = ${cfg.repoDir}
          ${lib.optionalString (config ? services.hermes-tools) "directory = ${config.services.hermes-tools.contentDir}"}
      '';
    };

    # Make ${cfg.repoDir} a real git checkout the bot can edit and roll back
    # against. The operator pushes a fresh copy (scripts/deploy.sh nix-copy),
    # and the box builds from its own checked-out repo on every
    # nixos-rebuild switch. Runs on EVERY activation: re-claims ownership and
    # git-inits once.
    #
    # Only state/ (root-owned rollback bookkeeping: last-known-good, gen-<N>
    # tags) must be excluded from the ledger — a `git reset --hard` must
    # never touch root-owned files the bot can't write.
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
      # ── Migration cleanup ────────────────────────────────────────────
      # Remove any leftover submodule state from the old git+file vendoring
      # era. On the first activation after this change, ${cfg.repoDir} may
      # still contain vendor/ directories with nested .git repos, plus
      # .gitmodules and gitlink index entries. This cleanup is idempotent.
      rm -rf "${cfg.repoDir}/vendor"
      rm -f "${cfg.repoDir}/.gitmodules"
      git -C ${cfg.repoDir} rm -r --cached vendor 2>/dev/null || true
      git submodule deinit -f --all 2>/dev/null || true
      # Ledger hygiene: state/ (root-owned rollback bookkeeping) must NOT be
      # tracked, so a `git reset --hard` in sync_repo_to_gen never touches it.
      grep -q '^state/$' ${cfg.repoDir}/.git/info/exclude 2>/dev/null \
        || printf 'state/\n' >> ${cfg.repoDir}/.git/info/exclude
      ${lib.getExe pkgs.git} -C ${cfg.repoDir} rm -r --cached state 2>/dev/null || true
    '';

    # Install the self-deploy callback backstop hook into the agent's hooks dir
    # ($HERMES_HOME/hooks/deploy-callback). The deployScript delivers the verdict
    # directly in the common path; this hook only catches the restart race where
    # that delivery could not complete before the new gateway booted. Editable
    # content-lane between deploys; re-installed (canonicalised) on each deploy.
    system.activationScripts.hermes-deploy-callback-hook = lib.stringAfter [ "hermes-agent-setup" ] ''
      HOOK_DIR=${hermesHome}/hooks/deploy-callback
      mkdir -p "$HOOK_DIR"
      install -o ${config.services.hermes-agent.user} -g ${config.services.hermes-agent.group} -m 0644 \
        ${deployCallbackHookYaml} "$HOOK_DIR/HOOK.yaml"
      install -o ${config.services.hermes-agent.user} -g ${config.services.hermes-agent.group} -m 0644 \
        ${deployCallbackHookHandler} "$HOOK_DIR/handler.py"
    '';

    # All systemd services in one merged definition: dynamically-generated
    # health check units + core deploy/rollback/onFailure units.
    systemd.services =
      let
        healthService = name: check: {
          "${"hermes-health-${name}"}" = {
            description = "health check: ${check.what}";
            wantedBy = [ ];
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
              wantedBy = [ ];
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
          hermes-deploy.wantedBy = [ ];
          hermes-deploy.path = [ config.system.path ];
          hermes-deploy.serviceConfig = {
            Type = "oneshot";
            # Fork into new session -> detached from systemd -> survives
            # activation cycle that would otherwise kill this unit mid-execution.
            # ">/dev/null 2>&1 < /dev/null" closes fds so the detached process
            # doesn't hold onto the old generation's ptmx/sockets.
            ExecStart = "${pkgs.util-linux}/bin/setsid ${deployScript} >/dev/null 2>&1 < /dev/null";
            TimeoutStartSec = 0;
            MemoryMax = cfg.switchMemoryMax;
          };
          hermes-rollback.description = "Roll back Hermes to the previous git/system generation";
          hermes-rollback.wantedBy = [ ];
          hermes-rollback.path = [ config.system.path ];
          hermes-rollback.serviceConfig = {
            Type = "oneshot";
            ExecStart = "${pkgs.util-linux}/bin/setsid ${rollbackScript} >/dev/null 2>&1 < /dev/null";
            TimeoutStartSec = 0;
            MemoryMax = cfg.switchMemoryMax;
          };
          "hermes-content-recovery-run".description =
            "Content-lane recovery (runs when hermes-agent/nicegui crashes)";
          "hermes-content-recovery-run".wantedBy = [ ];
          "hermes-content-recovery-run".path = [ config.system.path ];
          "hermes-content-recovery-run".serviceConfig = {
            Type = "oneshot";
            ExecStart = contentRecoveryScript;
            RemainAfterExit = true;
            MemoryMax = cfg.switchMemoryMax;
          };
          "hermes-content-recovery-run".onFailure = [ "hermes-rollback-run.service" ];
          "hermes-rollback-run".description = "Nix generation rollback (runs when content recovery fails)";
          "hermes-rollback-run".wantedBy = [ ];
          "hermes-rollback-run".path = [ config.system.path ];
          "hermes-rollback-run".serviceConfig = {
            Type = "oneshot";
            ExecStart = rollbackScript;
            MemoryMax = cfg.switchMemoryMax;
          };
          "hermes-nicegui-restart".description =
            "Simple restart for hermes-nicegui crashes (no Nix rollback)";
          "hermes-nicegui-restart".wantedBy = [ ];
          "hermes-nicegui-restart".path = [ config.system.path ];
          "hermes-nicegui-restart".serviceConfig = {
            Type = "oneshot";
            ExecStart = pkgs.writeShellScript "hermes-nicegui-restart-script" ''
              systemctl restart hermes-nicegui
            '';
          };
          "hermes-agent".onFailure = lib.mkIf (cfg.contentRecovery == "") (
            lib.mkForce [ "hermes-rollback-run.service" ]
          );
          "hermes-nicegui".onFailure = (lib.mkForce [ "hermes-nicegui-restart.service" ]);
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
        # hermes-deploy [--callback <session_id>]: when a session self-deploys
        # it hands its own id so the deploy can report back into that session
        # (via ${callbackFile}). No args = plain operator deploys, unchanged.
        (pkgs.writeShellScriptBin "hermes-deploy" ''
                    CALLBACK=""
                    while [ $# -gt 0 ]; do
                      case "$1" in
                        --callback)
                          CALLBACK="$2"; shift 2 ;;
                        --callback=*)
                          CALLBACK="''${1#--callback=}"; shift ;;
                        *)
                          echo "unknown option $1" >&2; exit 1 ;;
                      esac
                    done
                    if [ -n "$CALLBACK" ]; then
                      CALLBACK_FILE=${callbackFile}
                      mkdir -p "$(dirname "$CALLBACK_FILE")"
                      "${pkgs.python3}/bin/python3" - "$CALLBACK_FILE" "$CALLBACK" <<'PYEOF'
          import json, os, sys
          path, sid = sys.argv[1], sys.argv[2]
          d = {
              "session_id": sid,
              "session_key": os.environ.get("HERMES_SESSION_KEY", ""),
              "platform": os.environ.get("HERMES_SESSION_PLATFORM", "") or os.environ.get("HERMES_SESSION_SOURCE", ""),
              "source": os.environ.get("HERMES_SESSION_SOURCE", ""),
              "requested_at": __import__("datetime").datetime.now().isoformat(),
          }
          json.dump(d, open(path, "w"))
          PYEOF
                      chmod 0644 "$CALLBACK_FILE"
                      chown ${config.services.hermes-agent.user}:${config.services.hermes-agent.group} "$CALLBACK_FILE" 2>/dev/null || true
                    fi
                    exec systemctl start hermes-deploy.service
        '')
        (pkgs.writeShellScriptBin "hermes-rollback" ''
          exec systemctl start hermes-rollback.service
        '')
        (pkgs.writeShellScriptBin "hermes-status" ''
          echo "== git =="; git -C ${cfg.repoDir} log --oneline -5
          echo "== vendor inputs =="
          python3 -c '
import json
n = json.load(open("'''${cfg.repoDir}'''/flake.lock")).get("nodes", {})
for k in ("hermes-agent","hermes-nicegui-src","xaelwiki-src"):
    rev = n.get(k,{}).get("locked",{}).get("rev","?")
    print(f"  {k}: {rev}")
'
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
