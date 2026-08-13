# Home Assistant profile API server — multiplexed on the main gateway,
# exposed on its own port via a streaming reverse proxy.
#
# Why not a second gateway process? The hermes gateway's api_server platform
# is a single port-binding platform: it binds ONE port, owned by the default
# profile. A secondary profile cannot bind its own api_server port (the
# port-binding-platform + multiplexing conflict, SecondaryPortBindingConfigError),
# so "home assistant on a different port" is achieved in two steps:
#
#   1. Multiplex the `ha` profile on the main gateway's api_server. With
#      `gateway.multiplex_profiles = true` the gateway serves the default
#      profile plus every valid directory under $HERMES_HOME/profiles/ (see
#      `profiles_to_serve`), routing each under a `/p/<profile>/` URL prefix.
#      The listener lives on 127.0.0.1:${apiServerPort} (8443) and carries
#      both the default profile (bare paths) and `ha` (/p/ha/...).
#   2. `scripts/ha-profile-proxy.py` runs as a systemd unit terminating
#      :${proxyPort} (8444) and forwarding everything to
#      http://127.0.0.1:8443/p/ha/, streaming both directions so SSE works.
#
# The `ha` profile is bootstrapped declaratively (same layout `hermes profile
# create` produces): its own config.yaml — with
# `platforms.api_server.enabled: false` so it shares the default listener
# instead of trying to bind a port — and its own .env with the LLM provider
# keys shared from the main profile's environment files (hermes-env secret).
#
# Secrets:
#   * API_SERVER_KEY (min 16 chars) gates the whole api_server listener. It
#     lives in the DEFAULT profile's .env, so it is provided via
#     `apiServerKeyFile` (an agenix secret) which is appended to
#     services.hermes-agent.environmentFiles. It is deliberately NOT copied
#     into the `ha` profile's .env.
#   * Provider keys flow into the `ha` profile automatically from the main
#     profile's other environment files.

{
  config,
  pkgs,
  lib,
  ...
}:

let
  cfg = config.services.hermes-ha;
  agent = config.services.hermes-agent;

  deepConfigType = lib.types.mkOptionType {
    name = "hermes-ha-config-attrs";
    description = "Hermes YAML config (attrset), merged deeply via lib.recursiveUpdate.";
    check = builtins.isAttrs;
    merge = _loc: defs: lib.foldl' lib.recursiveUpdate { } (map (d: d.value) defs);
  };

  profileHome = "${agent.stateDir}/.hermes/profiles/${cfg.profile}";

  configJson = builtins.toJSON (
    lib.recursiveUpdate { terminal.cwd = cfg.workingDirectory; } cfg.settings
  );
  generatedConfigFile = pkgs.writeText "hermes-ha-config.yaml" configJson;

  configMergeScript = pkgs.writeScript "hermes-ha-config-merge" ''
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

  envFileContent = lib.concatStringsSep "\n" (
    lib.mapAttrsToList (k: v: "${k}=${v}") cfg.environment
  );

  # Provider/secret env files shared into the profile — everything EXCEPT the
  # api_server key file, which belongs only to the default profile's listener.
  profileEnvFiles = lib.filter (f: f != cfg.apiServerKeyFile) agent.environmentFiles;

  # The reverse proxy: python (with aiohttp) wrapping scripts/ha-profile-proxy.py.
  proxyPython = pkgs.python3.withPackages (ps: [ ps.aiohttp ]);
  proxyScript = pkgs.writeShellScript "hermes-ha-proxy" ''
    exec ${proxyPython}/bin/python3 ${./../scripts/ha-profile-proxy.py} "$@"
  '';
in
{
  options.services.hermes-ha = {
    enable = lib.mkEnableOption "Home Assistant profile API server (multiplexed + reverse-proxied on its own port)";

    profile = lib.mkOption {
      type = lib.types.str;
      default = "ha";
      description = "Hermes profile served under /p/<profile>/ and proxied on its own port.";
    };

    apiServerPort = lib.mkOption {
      type = lib.types.int;
      default = 8443;
      description = "Port the main gateway's api_server binds (single multiplexed listener).";
    };

    proxyPort = lib.mkOption {
      type = lib.types.int;
      default = 8444;
      description = "Port the reverse proxy terminates; this is what Home Assistant talks to.";
    };

    proxyBind = lib.mkOption {
      type = lib.types.str;
      default = "0.0.0.0";
      description = "Address the reverse proxy binds. Loopback only unless the firewall rule is desired.";
    };

    workingDirectory = lib.mkOption {
      type = lib.types.str;
      default = "${agent.stateDir}/ha-workspace";
      description = "Working directory (terminal.cwd) for the ha profile's agent.";
    };

    apiServerKeyFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        Agenix-decrypted env file holding API_SERVER_KEY (min 16 chars). It is
        appended to the DEFAULT profile's .env — the api_server listener needs
        it to start at all.
      '';
    };

    settings = lib.mkOption {
      type = deepConfigType;
      default = { };
      description = "Declarative Hermes config for the ha profile, deep-merged into its config.yaml.";
    };

    environment = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      description = "Extra non-secret env vars for the ha profile's .env.";
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "Optional agenix-decrypted env file with ha-profile secrets (appended to its .env).";
    };
  };

  config = lib.mkIf cfg.enable {
    # ── Main gateway: multiplex profiles on the api_server ─────────────
    services.hermes-agent.settings.gateway.multiplex_profiles = true;
    services.hermes-agent.environment = {
      API_SERVER_PORT = toString cfg.apiServerPort;
      API_SERVER_HOST = "127.0.0.1";
    };
    services.hermes-agent.environmentFiles = lib.optionals (cfg.apiServerKeyFile != null) [ cfg.apiServerKeyFile ];

    # Same declarative defaults as the main agent, scoped to the ha profile.
    services.hermes-ha.settings = {
      model.default = config.hermesDeploy.model;
      terminal.backend = "local";
      # The ha profile shares the default listener — never bind its own port.
      platforms.api_server.enabled = false;
    };

    # ── Reverse proxy (the "separate port") ────────────────────────────
    systemd.services.hermes-ha-proxy = {
      description = "Home Assistant profile API reverse proxy (${cfg.profile} → :${toString cfg.proxyPort})";
      wantedBy = [ "multi-user.target" ];
      after = [ "hermes-agent.service" "network-online.target" ];
      wants = [ "hermes-agent.service" "network-online.target" ];
      serviceConfig = {
        ExecStart = proxyScript;
        Restart = "always";
        RestartSec = 5;
        Environment = [
          "HA_PROFILE_PROXY_BIND=${cfg.proxyBind}"
          "HA_PROFILE_PROXY_PORT=${toString cfg.proxyPort}"
          "HA_PROFILE_PROXY_UPSTREAM=http://127.0.0.1:${toString cfg.apiServerPort}"
          "HA_PROFILE_PROXY_PREFIX=/p/${cfg.profile}"
        ];
      };
    };

    # Expose the proxy port on the tailnet (HA reaches hermesagent.lan:8444).
    networking.firewall.interfaces.tailscale0.allowedTCPPorts = [ cfg.proxyPort ];

    # ── ha profile bootstrap (declarative `hermes profile create`) ─────
    system.activationScripts."hermes-ha-setup" = lib.stringAfter [ "hermes-agent-setup" ] ''
      mkdir -p "${lib.escapeShellArg (dirOf profileHome)}"
      mkdir -p "${lib.escapeShellArg profileHome}"
      chown ${agent.user}:${agent.group} "${lib.escapeShellArg profileHome}"
      chmod 2770 "${lib.escapeShellArg profileHome}"
      for _subdir in memories sessions skills skins logs plans workspace cron home; do
        mkdir -p "${profileHome}/$_subdir"
        chown ${agent.user}:${agent.group} "${profileHome}/$_subdir"
        chmod 2770 "${profileHome}/$_subdir"
      done

      # config.yaml — declarative settings, Nix wins.
      ${configMergeScript} ${generatedConfigFile} "${lib.escapeShellArg (profileHome + "/config.yaml")}"
      chown ${agent.user}:${agent.group} "${lib.escapeShellArg (profileHome + "/config.yaml")}"
      chmod 0640 "${lib.escapeShellArg (profileHome + "/config.yaml")}"

      # .env — declarative env + shared provider keys + profile secrets.
      ENV_FILE="${profileHome}/.env"
      install -o ${agent.user} -g ${agent.group} -m 0640 /dev/null "$ENV_FILE"
      cat > "$ENV_FILE" <<'HERMES_HA_ENV_EOF'
      ${envFileContent}
      HERMES_HA_ENV_EOF
      ${lib.concatStringsSep "\n" (map (f: ''
        if [ -f "${f}" ]; then
          echo "" >> "$ENV_FILE"
          cat "${f}" >> "$ENV_FILE"
        fi
      '') profileEnvFiles)}
      ${lib.optionalString (cfg.environmentFile != null) ''
        if [ -f "${cfg.environmentFile}" ]; then
          echo "" >> "$ENV_FILE"
          cat "${cfg.environmentFile}" >> "$ENV_FILE"
        fi
      ''}
      chown ${agent.user}:${agent.group} "$ENV_FILE"

      # Working directory + managed marker.
      mkdir -p "${lib.escapeShellArg cfg.workingDirectory}"
      chown ${agent.user}:${agent.group} "${lib.escapeShellArg cfg.workingDirectory}"
      chmod 2770 "${lib.escapeShellArg cfg.workingDirectory}"

      touch "${lib.escapeShellArg (profileHome + "/.managed")}"
      chown ${agent.user}:${agent.group} "${lib.escapeShellArg (profileHome + "/.managed")}"
      chmod 0644 "${lib.escapeShellArg (profileHome + "/.managed")}"
    '';

    warnings = lib.optionals (cfg.apiServerKeyFile == null) [
      "services.hermes-ha: no apiServerKeyFile set — API_SERVER_KEY is missing and the api_server platform (and therefore /p/${cfg.profile}/) will not start."
    ];
  };
}
