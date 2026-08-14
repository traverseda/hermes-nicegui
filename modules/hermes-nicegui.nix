# hermes-nicegui — modular NiceGUI web UI for Hermes Agent.
#
# hermes-nicegui (https://github.com/traverseda/hermes-nicegui) is a browser
# UI for Hermes: a profile switcher, sessions/cron/kanban boards, a browser
# terminal, and a file browser. It is NOT the Hermes dashboard
# (modules/hermes-dashboard.nix runs `hermes dashboard`); it is a separate
# NiceGUI app that reaches Hermes by running the `hermes` CLI as a subprocess
# (HermesExecutor) — sessions, cron mutations, and chat all go through the
# CLI. Kanban talks to the dashboard's own web server instead (cookie login).
#
# Design — follows the xaelwiki "editable, low-stakes" pattern:
#   * The SOURCE ships as a git submodule (vendor/hermes-nicegui), pinned in
#     this repo. On the LXC it is materialized into the deploy checkout and
#     SYMLINKED into the run location (${stateDir}/src). The service runs the
#     checkout directly via a Nix python env — editing the submodule files
#     takes effect on `systemctl restart hermes-nicegui`, with NO
#     nixos-rebuild. The deploy flow runs `git submodule update --init`
#     (never --force), so local edits survive deploys, and rollback is
#     `git checkout <good-rev>` inside the submodule — independent of Nix.
#   * PLUGIN DISCOVERY WITHOUT A PIP INSTALL. hermes-nicegui discovers its
#     built-in plugins via the `hermes_nicegui.plugins` importlib.metadata
#     entry-point group (web.py::build → plugin.py::load_plugins). A bare
#     package install is the normal way entry points get registered, but that
#     would bake the source into the store and break the editable model. So
#     we ship a MINIMAL generated `hermes-nicegui-*.dist-info` (METADATA +
#     entry_points.txt) on PYTHONPATH next to the live checkout: importlib
#     discovers it as a distribution and resolves the entry-point values
#     against the real, editable modules. Verified with the app's own venv.
#     Keep entry_points.txt in sync with [project.entry-points] in the
#     submodule's pyproject.toml.
#   * IMMUTABLE PYTHON ENV, editable source. nicegui/httpx/loguru/
#     pydantic-settings/pyyaml/zstandard all come from nixpkgs; only the app
#     code is the mutable submodule.
#   * Runs AS THE hermes USER, colocated with the agent: the CLI subprocess,
#     profile switcher, and direct state reads (HermesExecutor reads
#     state.db / jobs.json straight from disk) all assume this app shares
#     $HERMES_HOME with the gateway. It is not a second gateway.
#   * EXPOSURE IS PUBLIC, via the Cloudflare tunnel, gated by the app's own
#     login. The unit binds 127.0.0.1 (HERMES_UI_HOST) and the remotely-
#     managed cloudflared tunnel on the same box routes a public hostname to
#     http://localhost:${port} — publishing that hostname is a Cloudflare
#     dashboard action, NOT a flake edit (see modules/cloudflare-tunnel.nix).
#     Binding loopback means no tailnet port is opened and the exposure
#     registry (modules/exposure.nix) is untouched. The app's username/
#     password login (HERMES_AUTH_ENABLED) is the auth gate — the same
#     guardrail-trade-off as the dashboard's public path.
#   * SECRETS from agenix: the kanban plugin logs into the Hermes dashboard
#     web server with a username/password (cookie session, needs the
#     PLAINTEXT dashboard password — the dashboard-env secret only holds the
#     scrypt hash, so a separate secret is required). Never in Nix config.

{
  config,
  pkgs,
  lib,
  ...
}:

let
  cfg = config.services.hermes-nicegui;
  agent = config.services.hermes-agent;

  # Immutable part: the python environment providing hermes-nicegui's
  # dependencies. The app code itself comes from the editable submodule
  # checkout via PYTHONPATH.
  pyEnv = pkgs.python312.withPackages (ps: [
    ps.nicegui
    ps.httpx
    ps.loguru
    ps.pydantic-settings
    ps.pyyaml
    ps.zstandard
  ]);

  # Minimal distribution metadata so importlib.metadata discovers the
  # built-in plugins without a pip install of the editable checkout.
  # Keep entry_points.txt in sync with pyproject.toml's
  # [project.entry-points."hermes_nicegui.plugins"].
  distInfo = pkgs.runCommand "hermes-nicegui-dist-info" { } ''
    mkdir -p "$out/hermes_nicegui-0.1.0.dist-info"
    cat > "$out/hermes_nicegui-0.1.0.dist-info/METADATA" <<'EOF'
    Metadata-Version: 2.1
    Name: hermes-nicegui
    Version: 0.1.0
    EOF
    echo "hermes_nicegui" > "$out/hermes_nicegui-0.1.0.dist-info/top_level.txt"
    cat > "$out/hermes_nicegui-0.1.0.dist-info/entry_points.txt" <<'EOF'
    [hermes_nicegui.plugins]
    sessions = hermes_nicegui.plugins.sessions:SessionsPlugin
    cron = hermes_nicegui.plugins.cron:CronPlugin
    terminal = hermes_nicegui.plugins.terminal:TerminalPlugin
    kanban = hermes_nicegui.plugins.kanban:KanbanPlugin
    files = hermes_nicegui.plugins.files:FilesPlugin
    EOF
  '';

  # Path of the submodule checkout inside the deploy repo on the LXC.
  submodulePath = "${config.services.hermes-deploy.repoDir}/vendor/hermes-nicegui";
in
{
  options.services.hermes-nicegui = {
    enable = lib.mkEnableOption "hermes-nicegui web UI";

    host = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = ''
        Address NiceGUI binds. Defaults to loopback: the public path is the
        colocated cloudflared tunnel routing a dashboard hostname to
        http://localhost:${toString cfg.port}, so nothing else needs to reach
        it directly.
        The app's own login (HERMES_AUTH_ENABLED) is the auth gate.
      '';
    };

    port = lib.mkOption {
      type = lib.types.int;
      default = 8080;
      description = "Port NiceGUI binds (point the tunnel hostname at http://localhost:${toString cfg.port}).";
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/hermes-nicegui";
      description = ''
        Service state: run-location symlink (${cfg.stateDir}/src), the app's
        HERMES_DATA_DIR (admin account + session-cookie secret). Owned by the
        hermes user.
      '';
    };

    dataDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/hermes-nicegui";
      description = ''
        HERMES_DATA_DIR: holds users.db (the admin account) and the generated
        storage_secret. Defaults to the state dir.
      '';
    };

    filesRoot = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/hermes/workspace";
      description = ''
        HERMES_FILES_ROOT: the root the files plugin confines browser
        read/write to. Defaults to the agent's workspace — the app is public
        (via the tunnel) and only gated by a single login, so do NOT widen
        this to a whole home directory.
      '';
    };

    kanbanUrl = lib.mkOption {
      type = lib.types.str;
      default = "http://127.0.0.1:9119";
      description = ''
        HERMES_KANBAN_URL: the Hermes dashboard web server the kanban plugin
        reads boards from (cookie login). Keep in sync with
        services.hermes-dashboard.port.
      '';
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        Agenix-decrypted env file with the kanban dashboard credentials:
          HERMES_KANBAN_USERNAME
          HERMES_KANBAN_PASSWORD   (PLAINTEXT — this is what the browser logs
                                   into the dashboard with; the dashboard-env
                                   secret only has the scrypt hash)
        Handed to the unit via systemd EnvironmentFile; no secrets in Nix.
      '';
    };

    authEnabled = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        HERMES_AUTH_ENABLED: gate the whole app behind the local
        username/password login (admin account created on first visit). This
        is the only thing standing between the public tunnel and a full file
        browser + terminal, so leave it on.
      '';
    };

    darkMode = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "HERMES_UI_DARK: render the UI in dark mode.";
    };

    logLevel = lib.mkOption {
      type = lib.types.str;
      default = "INFO";
      description = "HERMES_LOG_LEVEL (loguru).";
    };

    extraEnv = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      description = "Extra environment variables for the unit.";
    };
  };

  config = lib.mkIf cfg.enable {
    # ── Activation: materialize submodule + symlink into run location ───
    # Mirrors the xaelwiki pattern. `git submodule update --init` is only
    # invoked when the checkout is missing, never force-updated, so local
    # edits to the submodule survive deploys — that is the point.
    system.activationScripts."hermes-nicegui-source" = lib.stringAfter [ "users" ] ''
      deploy_repo=${lib.escapeShellArg config.services.hermes-deploy.repoDir}
      if [ -d "$deploy_repo/.git" ] && [ ! -e ${lib.escapeShellArg "${submodulePath}/.git"} ]; then
        echo "hermes-nicegui: initializing source submodule"
        git -C "$deploy_repo" submodule update --init vendor/hermes-nicegui || true
      fi
      mkdir -p ${cfg.stateDir}
      chown ${agent.user}:${agent.group} ${cfg.stateDir}
      chmod 0750 ${cfg.stateDir}
      ln -sfn ${lib.escapeShellArg submodulePath} ${cfg.stateDir}/src
      chown -h ${agent.user}:${agent.group} ${cfg.stateDir}/src
    '';

    # ── The web UI itself ───────────────────────────────────────────────
    # Runs the EDITABLE submodule checkout (via the run-location symlink)
    # with the immutable Nix python env. PYTHONPATH points at the checkout's
    # src/ package dir AND at the generated dist-info so importlib.metadata
    # discovers the built-in plugins. The `hermes` CLI subprocess (sessions/
    # cron/chat) uses the exact binary the agent runs, and shares
    # $HERMES_HOME so profile state resolves the same way.
    systemd.services.hermes-nicegui = {
      description = "hermes-nicegui web UI";
      wantedBy = [ "multi-user.target" ];
      after = [
        "hermes-agent.service"
        "hermes-dashboard.service"
        "network-online.target"
      ];
      wants = [
        "hermes-agent.service"
        "hermes-dashboard.service"
        "network-online.target"
      ];

      environment = {
        HOME = agent.stateDir;
        HERMES_HOME = "${agent.stateDir}/.hermes";
        HERMES_UI_HOST = cfg.host;
        HERMES_UI_PORT = toString cfg.port;
        # Reload mode forks a file-watcher subprocess — never in production.
        HERMES_UI_RELOAD = "false";
        HERMES_AUTH_ENABLED = lib.boolToString cfg.authEnabled;
        HERMES_UI_DARK = lib.boolToString cfg.darkMode;
        HERMES_EXEC_MODE = "local";
        # Absolute path, not the `hermes` name on PATH: a systemd unit's PATH
        # is not the interactive shell's, and this must not drift from the
        # agent's own binary.
        HERMES_CLI_BIN = "${agent.package}/bin/hermes";
        HERMES_KANBAN_URL = cfg.kanbanUrl;
        HERMES_DATA_DIR = cfg.dataDir;
        HERMES_FILES_ROOT = cfg.filesRoot;
        HERMES_LOG_LEVEL = cfg.logLevel;
        PYTHONPATH = "${cfg.stateDir}/src/src:${distInfo}";
      } // cfg.extraEnv;

      serviceConfig = {
        User = agent.user;
        Group = agent.group;
        WorkingDirectory = agent.workingDirectory;

        ExecStart = "${pyEnv}/bin/python ${cfg.stateDir}/src/main.py";

        # Kanban creds come from the agenix env file, as process env vars
        # (which win over any .env file the app also loads).
        EnvironmentFile = lib.optionals (cfg.environmentFile != null) [
          cfg.environmentFile
        ];

        Restart = "always";
        RestartSec = 5;

        # Same hardening as the other hermes units: shared state is
        # group-writable, no privilege escalation, no system writes outside
        # the state/workspace dirs.
        UMask = "0007";
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = false;
        ReadWritePaths = [
          cfg.stateDir
          cfg.dataDir
          agent.stateDir
          config.services.hermes-deploy.repoDir
        ];
        PrivateTmp = true;
      };

      path = with pkgs; [
        bash # the terminal plugin forks a shell pty
        coreutils
        git # the hermes CLI itself shells out to git
      ];
    };

    warnings = lib.optionals (cfg.environmentFile == null) [
      "services.hermes-nicegui: no environmentFile set — the kanban plugin cannot log into the Hermes dashboard without HERMES_KANBAN_USERNAME/_PASSWORD (the rest of the UI still works)."
    ];
  };
}
