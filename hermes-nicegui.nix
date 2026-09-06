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
# Design — vendored source, system lane (see README "Two change lanes"):
#   * The SOURCE ships as a git submodule (vendor/hermes-nicegui), fetched by
#     the `hermes-nicegui-src` flake input (git+file:./vendor/hermes-nicegui)
#     and built into the Nix store like any other flake input. Editing the
#     submodule requires a commit inside it, `nix flake lock --update-input
#     hermes-nicegui-src`, and a `nixos-rebuild switch` to take effect — there
#     is no live/editable checkout and no restart-to-apply. This is
#     deliberate: it's in-process application code, not low-stakes content, so
#     it gets the same commit -> switch -> generation rollback safety net as
#     everything else in the system lane. modules/hermes-deploy.nix's
#     generation-aware rollback resets vendor/hermes-nicegui's checkout (via
#     `git submodule update`) to match whichever generation is restored.
#   * PLUGIN DISCOVERY WITHOUT A PIP INSTALL. hermes-nicegui discovers its
#     built-in plugins via the `hermes_nicegui.plugins` importlib.metadata
#     entry-point group (web.py::build → plugin.py::load_plugins). Rather than
#     a full pip/setuptools build of the vendored source, we ship a MINIMAL
#     generated `hermes-nicegui-*.dist-info` (METADATA + entry_points.txt) on
#     PYTHONPATH next to appSrc: importlib discovers it as a distribution and
#     resolves the entry-point values against the real modules. Verified with
#     the app's own venv. Keep entry_points.txt in sync with
#     [project.entry-points] in the submodule's pyproject.toml.
#   * Both the python env (nicegui/httpx/loguru/pydantic-settings/pyyaml/
#     zstandard, from nixpkgs) AND the app code (appSrc) are immutable store
#     paths — nothing about this service is live-editable.
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
  hermes-nicegui-src,
  ...
}:

let
  cfg = config.services.hermes-nicegui;
  agent = config.services.hermes-agent;

  # Immutable app source: the vendored submodule, fetched and pinned by the
  # hermes-nicegui-src flake input. A plain Nix store path — no runtime
  # dependency on the deploy checkout.
  appSrc = hermes-nicegui-src;

  # The python environment providing hermes-nicegui's dependencies. The app
  # code itself comes from appSrc via PYTHONPATH.
  pyEnv = pkgs.python312.withPackages (ps: [
    ps.nicegui
    ps.httpx
    ps.loguru
    ps.pydantic-settings
    ps.pyyaml
    ps.zstandard
  ]);

  # Minimal distribution metadata so importlib.metadata discovers the
  # built-in plugins without a full pip/setuptools build of appSrc.
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
        Service state: the app's HERMES_DATA_DIR (admin account +
        session-cookie secret). Owned by the hermes user. The app source
        itself is immutable (built into the store from the hermes-nicegui-src
        flake input) and does not live here.
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

    gatewayTokenFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        Agenix-decrypted env file with HERMES_API_TOKEN, the bearer token the
        sessions/cron plugins send to the hermes gateway api_server
        (127.0.0.1:8443). Must match the gateway's API_SERVER_KEY (the
        api-server-env secret). Without it every gateway call 401s. Loaded as
        a second systemd EnvironmentFile; no secrets in Nix.
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
    # ── State dir: data only (admin account, session secret) — no source ──
    system.activationScripts."hermes-nicegui-state" = lib.stringAfter [ "users" ] ''
      mkdir -p ${cfg.stateDir}
      chown ${agent.user}:${agent.group} ${cfg.stateDir}
      chmod 0750 ${cfg.stateDir}
    '';

    # ── The web UI itself ───────────────────────────────────────────────
    # Runs the IMMUTABLE app source (appSrc, from the hermes-nicegui-src
    # flake input) with the Nix python env. PYTHONPATH points at the source's
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
        # Sessions created from the UI default to this model. Must be a valid
        # gateway model ID: the gateway falls back to "hermes-agent" (invalid)
        # when the request omits a model, which 400s on the first chat turn.
        HERMES_DEFAULT_MODEL = config.hermesDeploy.providers.${config.hermesDeploy.defaultProvider}.model;
        # One shared flag changes model + provider for hermes, hindsight, nicegui, ha.
        HERMES_DEFAULT_PROVIDER = config.hermesDeploy.providers.${config.hermesDeploy.defaultProvider}.provider;
        HERMES_KANBAN_URL = cfg.kanbanUrl;
        HERMES_DATA_DIR = cfg.dataDir;
        HERMES_FILES_ROOT = cfg.filesRoot;
        HERMES_LOG_LEVEL = cfg.logLevel;
        PYTHONPATH = "${appSrc}/src:${distInfo}";
      } // cfg.extraEnv;

      serviceConfig = {
        User = agent.user;
        Group = agent.group;
        WorkingDirectory = agent.workingDirectory;

        ExecStart = "${pyEnv}/bin/python ${appSrc}/main.py";

        # Kanban creds + gateway bearer token come from agenix env files, as
        # process env vars (which win over any .env file the app also loads).
        EnvironmentFile = lib.optionals (cfg.environmentFile != null) [
          cfg.environmentFile
        ] ++ lib.optionals (cfg.gatewayTokenFile != null) [
          cfg.gatewayTokenFile
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
