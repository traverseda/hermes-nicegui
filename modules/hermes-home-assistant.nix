# Second Hermes gateway — an OpenAI-compatible API server on its own port,
# running under a dedicated Hermes profile ("home-assistant" by default).
#
# The upstream `services.hermes-agent` NixOS module manages exactly one
# gateway (the main profile, HERMES_HOME=${stateDir}/.hermes). Hermes itself
# supports running extra profiles side by side: each profile has its own
# HERMES_HOME under <root>/profiles/<name>/, its own config.yaml, .env,
# sessions, skills and memory — and its own gateway PID/lock, so a second
# process is fully independent.
#
# This module bootstraps a profile directory declaratively (mirroring what
# `hermes profile create` would do) and runs `hermes gateway` against it. The
# OpenAI-compatible API server platform is enabled automatically because the
# profile's .env sets API_SERVER_KEY (any usable key auto-enables it — see
# gateway/config.py). Port and bind host come from API_SERVER_PORT /
# API_SERVER_HOST in the profile .env.
#
# Secrets:
#   * Provider keys (LLM) are shared with the main profile by appending its
#     configured `services.hermes-agent.environmentFiles` (the hermes-env
#     agenix secret) to this profile's .env.
#   * API_SERVER_KEY must be provided via `environmentFile` — an agenix
#     secret (e.g. secrets/home-assistant-env.age) — min length 16.
#
# Like everything else here, the profile's config.yaml and .env are written
# from Nix on activation, so the agent's self-edits remain rollback-safe.

{
  config,
  pkgs,
  lib,
  ...
}:

let
  cfg = config.services.hermes-home-assistant;
  agent = config.services.hermes-agent;

  # Deep-merge config type, same semantics as the upstream hermes-agent module.
  deepConfigType = lib.types.mkOptionType {
    name = "hermes-home-assistant-config-attrs";
    description = "Hermes YAML config (attrset), merged deeply via lib.recursiveUpdate.";
    check = builtins.isAttrs;
    merge = _loc: defs: lib.foldl' lib.recursiveUpdate { } (map (d: d.value) defs);
  };

  # Profile root: <stateDir>/.hermes/profiles/<profile> — this is HERMES_HOME
  # for the second gateway.
  profileHome = "${cfg.stateDir}/.hermes/profiles/${cfg.profile}";

  isLoopback = lib.elem cfg.host [ "127.0.0.1" "::1" "localhost" ];

  # Nix-declared settings, deep-merged into the profile's config.yaml.
  configJson = builtins.toJSON (
    lib.recursiveUpdate { terminal.cwd = cfg.workingDirectory; } cfg.settings
  );
  generatedConfigFile = pkgs.writeText "hermes-home-assistant-config.yaml" configJson;

  # Merge Nix settings into the existing profile config.yaml, preserving keys
  # the agent adds itself (Nix keys win). Mirrors upstream configMergeScript.
  configMergeScript = pkgs.writeScript "hermes-home-assistant-config-merge" ''
    #!${pkgs.python3.withPackages (ps: [ ps.pyyaml ])}/bin/python3
    import json, yaml, sys
    from pathlib import Path

    with open(sys.argv[1]) as f:
        nix = json.load(f)
    config_path = Path(sys.argv[2])

    existing = {}
    if config_path.exists():
        with open(config_path) as f:
            existing = yaml.safe_load(f) or {}

    def deep_merge(base, override):
        result = dict(base)
        for k, v in override.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = deep_merge(result[k], v)
            else:
                result[k] = v
        return result

    with open(config_path, "w") as f:
        yaml.dump(deep_merge(existing, nix), f, default_flow_style=False, sort_keys=False)
  '';

  # Non-secret env vars written to the profile's .env (secrets come from the
  # appended environment files).
  envFileContent = lib.concatStringsSep "\n" (
    lib.mapAttrsToList (k: v: "${k}=${v}") cfg.environment
  );
in
{
  options.services.hermes-home-assistant = {
    enable = lib.mkEnableOption "second Hermes gateway — an OpenAI-compatible API server on a separate profile and port";

    profile = lib.mkOption {
      type = lib.types.str;
      default = "home-assistant";
      description = "Hermes profile the API server runs under (directory under <stateDir>/.hermes/profiles/).";
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/hermes";
      description = "Base state directory shared with services.hermes-agent; the profile lives at <stateDir>/.hermes/profiles/<profile>.";
    };

    workingDirectory = lib.mkOption {
      type = lib.types.str;
      default = "${cfg.stateDir}/home-assistant";
      description = "Working directory (terminal.cwd) for the API-server agent — kept separate from the main profile's workspace.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "hermes";
      description = "System user running the gateway (must match services.hermes-agent.user).";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "hermes";
      description = "System group running the gateway.";
    };

    port = lib.mkOption {
      type = lib.types.int;
      default = 8643;
      description = "Port the OpenAI-compatible API server binds (API_SERVER_PORT in the profile .env).";
    };

    host = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = ''
        Bind host for the API server (API_SERVER_HOST). Loopback by default.
        Set to "0.0.0.0" to expose it on the tailnet (the port is then opened
        on tailscale0 only).
      '';
    };

    settings = lib.mkOption {
      type = deepConfigType;
      default = { };
      description = "Declarative Hermes config for the profile, deep-merged into its config.yaml.";
    };

    environment = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      description = "Extra non-secret environment variables for the profile's .env.";
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        Agenix-decrypted env file with secrets for this profile. Must provide
        API_SERVER_KEY (min 16 chars) — the API server refuses to start
        without it. Provider keys are inherited from the main profile's
        environmentFiles automatically.
      '';
    };

    extraArgs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Extra command-line arguments for `hermes gateway`.";
    };
  };

  config = lib.mkIf cfg.enable {
    # Same declarative defaults as the main agent, scoped to this profile.
    # Override via services.hermes-home-assistant.settings in the host config.
    services.hermes-home-assistant.settings = {
      model.default = config.hermesDeploy.model;
      terminal.backend = "local";
    };

    # Only reachable over the tailnet when we bind off loopback.
    networking.firewall.interfaces.tailscale0.allowedTCPPorts = lib.mkIf (!isLoopback) [ cfg.port ];

    system.activationScripts."hermes-home-assistant-setup" = lib.stringAfter [ "hermes-agent-setup" ] ''
      # ── Profile directory (HERMES_HOME) ──────────────────────────────
      # Same bootstrap as `hermes profile create` (subdirs from _PROFILE_DIRS).
      mkdir -p "${lib.escapeShellArg (dirOf profileHome)}"
      mkdir -p "${lib.escapeShellArg profileHome}"
      chown ${cfg.user}:${cfg.group} "${lib.escapeShellArg profileHome}"
      chmod 2770 "${lib.escapeShellArg profileHome}"
      for _subdir in memories sessions skills skins logs plans workspace cron home; do
        mkdir -p "${profileHome}/$_subdir"
        chown ${cfg.user}:${cfg.group} "${profileHome}/$_subdir"
        chmod 2770 "${profileHome}/$_subdir"
      done

      # ── config.yaml — declarative settings, Nix wins ─────────────────
      ${configMergeScript} ${generatedConfigFile} "${lib.escapeShellArg (profileHome + "/config.yaml")}"
      chown ${cfg.user}:${cfg.group} "${lib.escapeShellArg (profileHome + "/config.yaml")}"
      chmod 0640 "${lib.escapeShellArg (profileHome + "/config.yaml")}"

      # ── .env — declarative env + shared provider keys + secrets ──────
      ENV_FILE="${profileHome}/.env"
      install -o ${cfg.user} -g ${cfg.group} -m 0640 /dev/null "$ENV_FILE"
      cat > "$ENV_FILE" <<'HERMES_HA_ENV_EOF'
      ${envFileContent}
      HERMES_HA_ENV_EOF
      echo "API_SERVER_PORT=${toString cfg.port}" >> "$ENV_FILE"
      echo "API_SERVER_HOST=${cfg.host}" >> "$ENV_FILE"
      # Provider keys shared with the main profile (hermes-env agenix secret).
      ${lib.concatStringsSep "\n" (map (f: ''
        if [ -f "${f}" ]; then
          echo "" >> "$ENV_FILE"
          cat "${f}" >> "$ENV_FILE"
        fi
      '') agent.environmentFiles)}
      # Profile-specific secrets (API_SERVER_KEY, …).
      ${lib.optionalString (cfg.environmentFile != null) ''
        if [ -f "${cfg.environmentFile}" ]; then
          echo "" >> "$ENV_FILE"
          cat "${cfg.environmentFile}" >> "$ENV_FILE"
        fi
      ''}
      chown ${cfg.user}:${cfg.group} "$ENV_FILE"

      # ── Working directory (isolated from the main profile) ───────────
      mkdir -p "${lib.escapeShellArg cfg.workingDirectory}"
      chown ${cfg.user}:${cfg.group} "${lib.escapeShellArg cfg.workingDirectory}"
      chmod 2770 "${lib.escapeShellArg cfg.workingDirectory}"

      # Managed marker, same as the main profile.
      touch "${lib.escapeShellArg (profileHome + "/.managed")}"
      chown ${cfg.user}:${cfg.group} "${lib.escapeShellArg (profileHome + "/.managed")}"
      chmod 0644 "${lib.escapeShellArg (profileHome + "/.managed")}"
    '';

    systemd.services.hermes-home-assistant = {
      description = "Hermes Agent Gateway (${cfg.profile} profile — OpenAI-compatible API server)";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];

      environment = {
        HOME = cfg.stateDir;
        HERMES_HOME = profileHome;
        HERMES_MANAGED = "true";
      };

      serviceConfig = {
        User = cfg.user;
        Group = cfg.group;
        WorkingDirectory = cfg.workingDirectory;

        ExecStart = lib.concatStringsSep " " ([
          "${agent.package}/bin/hermes"
          "gateway"
        ] ++ cfg.extraArgs);

        Restart = "always";
        RestartSec = 5;

        # Same hardening as the main gateway unit.
        UMask = "0007";
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = false;
        ReadWritePaths = [
          cfg.stateDir
          cfg.workingDirectory
        ];
        PrivateTmp = true;
      };

      path = [
        agent.package
        pkgs.bash
        pkgs.coreutils
        pkgs.git
      ] ++ agent.extraPackages;
    };

    warnings = lib.optionals (cfg.environmentFile == null) [
      "services.hermes-home-assistant: no environmentFile set — API_SERVER_KEY is missing and the API server platform will not start (it requires a usable key, min 16 chars)."
    ];
  };
}
