# hermes-emergency-recovery — periodic emergency recovery for hermes-agent / nicegui.
#
# This is the final safety net: a systemd timer fires every 30 minutes, runs
# a health check on the gateway + nicegui, and if either is unhealthy it
# launches opencode (via its system PATH binary) with a recovery prompt.
# Opencode diagnoses and attempts to fix the situation, then writes a
# post-mortem kanban ticket. Everything is Nix-declared, no crontab, no
# hand-edited scripts.

{ config, pkgs, lib, ... }:

let
  cfg = config.services.hermes-emergency-recovery;
  agentUser = config.services.hermes-agent.user;
  hermesHome = config.services.hermes-agent.stateDir + "/.hermes";

  # ── Recovery script ──────────────────────────────────────────────────
  # Runs as the hermes user (User=hermes in the unit), so opencode has
  # access to HERMES_HOME, kanban tools, and the agent environment.
  recoveryScript = pkgs.writeShellScript "hermes-emergency-recovery" ''
    set -uo pipefail

    LOG="/var/log/hermes-emergency-recovery.log"
    TIMESTAMP=$(date -Iseconds)
    LOGFILE="$LOG.$(date +%Y%m%d)"

    # Ensure the daily log file exists and is writable (root writes via
    # systemd's LogsDirectory=/var/log/hermes-recovery but we log to
    # /var/log for operator convenience; ProtectSystem=strict means we
    # can't touch /var/log at runtime without ReadWritePaths).
    touch "$LOGFILE" 2>/dev/null || true
    # Ensure the log dir is writable by root (ProtectSystem=strict +
    # ReadWritePaths includes /var/log)
    log() { echo "[$TIMESTAMP] $*" | tee -a "$LOGFILE"; }

    log "=== Emergency recovery check started ==="

    # ── Health checks ──────────────────────────────────────────────────
    FAILURES=""

    # Check 1: hermes-agent systemd unit
    if ! systemctl is-active --quiet hermes-agent 2>/dev/null; then
      FAILURES="$FAILURES hermes-agent is NOT active"
      log "FAIL: hermes-agent is NOT active"
    else
      # Check 2: gateway API responds
      GW_HTTP=$(curl -sf --max-time 10 -o /dev/null -w "%{http_code}" http://127.0.0.1:8443/health 2>/dev/null || echo "000")
      if [ "$GW_HTTP" != "200" ] && [ "$GW_HTTP" != "401" ]; then
        FAILURES="$FAILURES hermes-agent API returned $GW_HTTP"
        log "FAIL: hermes-agent API returned $GW_HTTP"
      else
        log "OK: hermes-agent active + API healthy"
      fi
    fi

    # Check 3: hermes-nicegui systemd unit
    if ! systemctl is-active --quiet hermes-nicegui 2>/dev/null; then
      FAILURES="$FAILURES hermes-nicegui is NOT active"
      log "FAIL: hermes-nicegui is NOT active"
    else
      # Check 4: nicegui responds
      NG_HTTP=$(curl -sf --max-time 10 -o /dev/null -w "%{http_code}" http://127.0.0.1:${toString cfg.niceguiPort}/ 2>/dev/null || echo "000")
      if [ "$NG_HTTP" = "000" ] || [ "$NG_HTTP" = "502" ] || [ "$NG_HTTP" = "503" ]; then
        FAILURES="$FAILURES hermes-nicegui returned $NG_HTTP"
        log "FAIL: hermes-nicegui returned $NG_HTTP"
      else
        log "OK: hermes-nicegui active + HTTP $NG_HTTP"
      fi
    fi

    # ── All checks passed — nothing to do ──────────────────────────────
    if [ -z "$FAILURES" ]; then
      log "All checks passed — exiting"
      exit 0
    fi

    # ── At least one failure — attempt recovery via opencode ───────────
    log "RECOVERY NEEDED:$FAILURES"

    # Gather diagnostic info
    AGENT_STATUS=$(systemctl status hermes-agent --no-pager 2>&1 | head -30 || true)
    NICEGUI_STATUS=$(systemctl status hermes-nicegui --no-pager 2>&1 | head -30 || true)
    AGENT_LOG=$(journalctl -u hermes-agent --no-pager -n 50 --since "30 min ago" 2>/dev/null | tail -50 || true)
    NICEGUI_LOG=$(journalctl -u hermes-nicegui --no-pager -n 50 --since "30 min ago" 2>/dev/null | tail -50 || true)
    UPTIME=$(uptime -p 2>/dev/null || uptime)
    DISK=$(df -h / 2>/dev/null | tail -1 || true)
    MEM=$(free -h 2>/dev/null | grep "^Mem:" || true)

    # Attempt a quick restart for simple failures before escalation
    QUICK_FIX=""
    if echo "$FAILURES" | grep -q "hermes-agent"; then
      log "Attempting quick restart of hermes-agent..."
      systemctl restart hermes-agent 2>&1 | tee -a "$LOGFILE"
      sleep 5
      if systemctl is-active --quiet hermes-agent; then
        QUICK_FIX="hermes-agent restart resolved"
        log "OK: hermes-agent recovered after restart"
      fi
    fi
    if echo "$FAILURES" | grep -q "hermes-nicegui"; then
      log "Attempting quick restart of hermes-nicegui..."
      systemctl restart hermes-nicegui 2>&1 | tee -a "$LOGFILE"
      sleep 5
      if systemctl is-active --quiet hermes-nicegui; then
        QUICK_FIX="hermes-nicegui restart resolved"
        log "OK: hermes-nicegui recovered after restart"
      fi
    fi

    # Re-check after quick fixes
    REMAINING_FAILURES=""
    if ! systemctl is-active --quiet hermes-agent 2>/dev/null; then
      REMAINING_FAILURES="$REMAINING_FAILURES hermes-agent is NOT active"
    fi
    NG_HTTP2=$(curl -sf --max-time 10 -o /dev/null -w "%{http_code}" http://127.0.0.1:${toString cfg.niceguiPort}/ 2>/dev/null || echo "000")
    if [ "$NG_HTTP2" = "000" ] || [ "$NG_HTTP2" = "502" ] || [ "$NG_HTTP2" = "503" ]; then
      REMAINING_FAILURES="$REMAINING_FAILURES hermes-nicegui returned $NG_HTTP2"
    fi

    if [ -n "$REMAINING_FAILURES" ] || [ -z "$QUICK_FIX" ]; then
      # ── Escalate to opencode ─────────────────────────────────────────
      log "Escalating to opencode for diagnosis and recovery..."

      # Write diagnostic context to a temp file opencode can read
      DIALOG_CONTEXT=$(mktemp /tmp/emergency-XXXXXX.ctx)
      cat > "$DIALOG_CONTEXT" <<EMERGENCY_EOF
# Emergency Recovery Context — $TIMESTAMP

## Failures detected:$FAILURES

## Quick fix results:
$QUICK_FIX

## System state:
- Uptime: $UPTIME
- Disk: $DISK
- Memory: $MEM

## hermes-agent logs (last 50 lines):
$AGENT_LOG

## hermes-nicegui logs (last 50 lines):
$NICEGUI_LOG

## hermes-agent service status:
$AGENT_STATUS

## hermes-nicegui service status:
$NICEGUI_STATUS

## Instructions
1. Diagnose the root cause of these failures.
2. Attempt to recover the affected service(s).
3. If recovery succeeds, write a summary to /tmp/emergency-recovery-success.txt.
4. Create a kanban ticket titled "EMERGENCY: <brief description of issue>" with
   assignee "default", body containing:
   - When the outage was detected
   - What failed
   - What was done to recover
   - Root cause analysis if identifiable
   - Recommendations to prevent recurrence
5. Write the kanban ticket details to /tmp/emergency-ticket-summary.txt.
6. Clean up temp files.
EMERGENCY_EOF

      # Run opencode with the diagnostic context
      set +e
      opencode --cli --accept-hooks chat -f "$DIALOG_CONTEXT" -q "You are in emergency recovery mode. Diagnose and fix the issues described above, then create a kanban ticket documenting the incident." 2>&1 | tee -a "$LOGFILE"
      OPencode_EXIT=$?
      set -e

      rm -f "$DIALOG_CONTEXT"

      if [ $OPencode_EXIT -eq 0 ]; then
        log "opencode completed successfully"
      else
        log "FAIL: opencode exited with code $OPencode_EXIT"
      fi
    fi

    # ── Summary ────────────────────────────────────────────────────────
    if [ -n "$QUICK_FIX" ]; then
      log "Recovery summary: $QUICK_FIX"
    elif [ -z "$FAILURES" ] || [ -z "$REMAINING_FAILURES" ]; then
      log "All services recovered"
    else
      log "WARN: unresolved failures: $REMAINING_FAILURES"
    fi

    log "=== Emergency recovery check finished ==="
  '';

in
{
  options.services.hermes-emergency-recovery = {
    enable = lib.mkEnableOption "hermes emergency recovery timer";

    # Port nicegui binds — must match services.hermes-nicegui.port (default 8080)
    niceguiPort = lib.mkOption {
      type = lib.types.int;
      default = 8080;
      description = "Port hermes-nicegui binds to, for HTTP health checks.";
    };
  };

  config = lib.mkIf cfg.enable {
    # ── Recovery systemd unit ──────────────────────────────────────────
    systemd.services.hermes-emergency-recovery = {
      description = "Hermes emergency recovery checker";
      wantedBy = [ ];
      partOf = [ "hermes-emergency-recovery-timer.service" ];

      # Must run after both services are up (they aren't, but this ensures
      # the units exist in the dependency graph).
      after = [ "hermes-agent.service" "hermes-nicegui.service" ];
      requires = [ ];  # Don't prevent timer from firing

      serviceConfig = {
        Type = "oneshot";
        # Run as root — we need full systemctl control to restart
        # hermes-agent / hermes-nicegui (no sudo needed when already root).
        User = "root";
        Group = "root";
        ExecStart = "${recoveryScript}";
        # Don't let the recovery script wedge the system — hard timeout.
        TimeoutStartSec = 600;  # 10 minutes max
        StateDirectory = "hermes-recovery";
        LogsDirectory = "hermes-recovery";
        PrivateTmp = true;
        ProtectSystem = "strict";
        ReadWritePaths = "${hermesHome} /var/log";
        # Inherit the hermes-agent PATH (includes opencode) so recovery
        # commands work without relying on /etc/profile or a broken Path key.
        # Opencode lives in the hermes user's profile (not a system package),
        # so we must include it in the service PATH.  Running as root means
        # /etc/profiles/per-user/hermes is NOT on PATH automatically.
        Environment = "PATH=/etc/profiles/per-user/hermes/bin:/run/wrappers/bin:/run/current-system/sw/bin:${pkgs.systemd}/bin:${pkgs.coreutils}/bin:${pkgs.findutils}/bin:${pkgs.gnused}/bin:${pkgs.gnugrep}/bin:${pkgs.curl}/bin:${pkgs.jq}/bin:${pkgs.bashInteractive}/bin:${pkgs.git}/bin:${pkgs.age}/bin";
      };

      environment = {
        HERMES_HOME = hermesHome;
      };
    };

    # ── Timer unit ─────────────────────────────────────────────────────
    systemd.timers."hermes-emergency-recovery-timer" = {
      description = "Hermes emergency recovery checker timer (30min)";
      wantedBy = [ "timers.target" ];
      # 30-minute interval
      timerConfig.OnBootSec = "5min";   # First run 5min after boot (avoid startup race)
      timerConfig.OnUnitActiveSec = "30min";
      # Randomize slightly to avoid thundering herd if multiple timers exist
      timerConfig.RandomizedSec = "2min";
    };

    # Expose `hermes-recovery` CLI for manual trigger runs.
    environment.systemPackages = [
      (pkgs.writeShellScriptBin "hermes-recovery" ''
        exec systemctl start hermes-emergency-recovery.service
      '')
    ];
  };
}
