# hermes-vnc — loopback-only Xvfb virtual display + x11vnc for
# human-in-the-loop tasks.
#
# The bot's browser runs on a virtual display. Occasionally a human operator
# needs to watch that browser and click through captchas, so the bot starts
# this service ON DEMAND (`systemctl start hermes-vnc`), the hermes-nicegui
# plugin serves a noVNC viewer over the app's own login + single-use token,
# and the bot stops the service when the task is done.
#
# Security model:
#   * LOOPBACK-ONLY: x11vnc binds 127.0.0.1 (-localhost) and Xvfb runs with
#     -nolisten tcp, so no port is opened on any interface. The only path to
#     the display is through the nicegui app (app login + single-use token).
#   * ON DEMAND, never at boot: the unit has NO wantedBy/requiredBy. If it
#     were in multi-user.target, Xvfb would run (and a live password file
#     exist) 24/7 for a task that happens rarely.
#   * Per-start password: the RFB password is generated fresh in the
#     RuntimeDirectory on every start and wiped on stop. The file is 0600 and
#     hermes-owned (RuntimeDirectory is created owned by User before
#     ExecStartPre runs).
#   * Runs as the hermes user: Xvfb/x11vnc need no privileges, port 5901 >
#     1024, and the app + plugin run as the same user so file ownership of
#     state.json + the browser profile is trivial.

{
  config,
  pkgs,
  lib,
  ...
}:

let
  cfg = config.services.hermes-vnc;
  agent = config.services.hermes-agent;

  # Display ":99" → X socket "/tmp/.X11-unix/X99" (strip the leading colon).
  xSocket = "/tmp/.X11-unix/X${lib.removePrefix ":" cfg.display}";

  # The per-start password lives in the RuntimeDirectory (/run/...), which is
  # wiped on stop — that is the point: the password dies with the service.
  runtimePath = "/run/${cfg.runtimeDir}";

  # Writes a fresh RFB password per start. RFB/VNC auth truncates to 8 chars
  # so this is defense-in-depth only; the real gates are the app login +
  # single-use token + loopback binding.
  passwdScript = pkgs.writeShellScript "hermes-vnc-passwd" ''
    umask 077
    tr -dc 'A-Za-z0-9' </dev/urandom | head -c 8 > ${runtimePath}/passwd
  '';

  # Xvfb in the background; x11vnc in the foreground (the service's main
  # process — KillMode=control-group reaps the Xvfb child on stop).
  runScript = pkgs.writeShellScript "hermes-vnc-run" ''
    #!/bin/sh
    set -eu
    Xvfb ${cfg.display} -screen 0 ${cfg.screen} -nolisten tcp &
    XVFB_PID=$!
    trap 'kill "$XVFB_PID" 2>/dev/null || true' EXIT INT TERM
    # Wait for the X socket (bwrap needs it to exist before the browser
    # starts).
    i=0
    while [ ! -S ${xSocket} ]; do
      i=$((i+1))
      [ "$i" -ge 100 ] && { echo "timed out waiting for X socket ${xSocket}" >&2; exit 1; }
      sleep 0.1
    done
    # The sandboxed browser runs as uid 65534; the X socket must be reachable
    # by it. The display is loopback-only (no -listen tcp on Xvfb) and shows
    # only the bot's own browser, so this is safe.
    chmod 0666 ${xSocket} 2>/dev/null || true
    exec x11vnc -display ${cfg.display} -localhost \
      -rfbauth ${runtimePath}/passwd -rfbport ${toString cfg.rfbPort} \
      -shared -forever -noxdamage
  '';
in
{
  options.services.hermes-vnc = {
    enable = lib.mkEnableOption "hermes-vnc loopback display service";

    display = lib.mkOption {
      type = lib.types.str;
      default = ":99";
      description = ''
        X display the virtual screen uses (":99" → socket
        /tmp/.X11-unix/X99). The nicegui plugin's settings must match.
      '';
    };

    screen = lib.mkOption {
      type = lib.types.str;
      default = "1600x1000x24";
      description = "Xvfb screen geometry WxHxD for the virtual display.";
    };

    rfbPort = lib.mkOption {
      type = lib.types.port;
      default = 5901;
      description = "x11vnc RFB port, bound to 127.0.0.1 only.";
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/hermes-vnc";
      description = ''
        Service state: state.json (the help-request contract the nicegui
        plugin reads/writes) + the browser profile. Created by
        StateDirectory, owned by the hermes user.
      '';
    };

    runtimeDir = lib.mkOption {
      type = lib.types.str;
      default = "hermes-vnc";
      description = "RuntimeDirectory name under /run (holds the per-start password).";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.services.hermes-vnc = {
      description = "Xvfb virtual display + loopback-only x11vnc for human-in-the-loop tasks";

      # NO wantedBy / NO requiredBy — the unit is started ON DEMAND by the bot
      # (`systemctl start hermes-vnc`) and stopped when the task is done. It
      # must NOT come up with multi-user.target: that would keep Xvfb and a
      # live password file running 24/7 for a task that happens rarely.
      after = [ "hermes-agent.service" ];

      path = with pkgs; [
        xvfb
        x11vnc
        coreutils
      ];

      serviceConfig = {
        # Xvfb/x11vnc need no privileges; port 5901 > 1024; the app + plugin
        # run as the same user so file ownership is trivial.
        User = agent.user;
        Group = agent.group;

        # state.json + the browser profile live here, owned by hermes.
        # Mode 0755 (not 0750): bwrap 0.11+ resolves bind sources as the
        # sandbox target uid (nobody) — it must be able to TRAVERSE this
        # dir to reach the profile bind source inside. Only local users
        # (hermes/root) exist on this box; state.json itself is 0644 and
        # holds only a help-request message/url, no secrets.
        StateDirectory = "hermes-vnc";
        StateDirectoryMode = "0755";

        # The per-start password file lives here (wiped on stop).
        RuntimeDirectory = cfg.runtimeDir;
        RuntimeDirectoryMode = "0750";

        ExecStartPre = passwdScript;
        ExecStart = runScript;

        Restart = "on-failure";
        RestartSec = 3;

        NoNewPrivileges = true;
        UMask = "0077";
        # Explicit: the wrapper's Xvfb child must die with the unit.
        KillMode = "control-group";
        # Deliberately NO PrivateTmp (the X socket must live on the REAL /tmp
        # so the bot's sandboxed browser can bind it) and NO
        # ProtectSystem=strict (it would make /tmp read-only for the unit).
      };
    };

    # bubblewrap is the bot's browser sandbox (the vnc-help helper drives
    # it), xdpyinfo/xwininfo/xwd/netpbm are for operator verification and
    # screenshots of the virtual display (DISPLAY=:99 xdpyinfo;
    # xwininfo -root -tree; xwd -root | xwdtopnm | pnmtopng). chromium is
    # the sandboxed browser the bot launches on the virtual display for
    # human-in-the-loop tasks — pinned in the system closure so the helper
    # never depends on a GC'd store path and the content-store `chromium`
    # symlinks always resolve.
    environment.systemPackages = [
      pkgs.bubblewrap
      pkgs.xdpyinfo
      pkgs.xorg.xwininfo
      pkgs.xorg.xwd
      pkgs.netpbm
      pkgs.chromium
    ];
  };
}
