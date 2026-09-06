# Hermes web dashboard (`hermes dashboard`) — exposed tailnet-only.
#
# The dashboard is the full web admin panel for the agent (sessions,
# messaging channels, MCP catalog, webhooks, memory, profile builder, plus
# an embedded chat tab). It is a standalone long-lived web server that
# shares $HERMES_HOME with the gateway; it is NOT a second gateway.
#
# Auth model:
#   * Binding to a non-loopback host (the default here is 0.0.0.0) engages
#     the dashboard's own auth gate — a public bind ALWAYS requires a
#     registered auth provider (the June 2026 hardening removed the
#     unauthenticated-public-dashboard escape hatch).
#   * We use the bundled `basic` provider (username/password, stateless
#     HMAC-signed sessions). Credentials live in an agenix env file
#     (HERMES_DASHBOARD_BASIC_AUTH_USERNAME / _PASSWORD_HASH / _SECRET),
#     appended to the default profile's .env exactly like the API_SERVER_KEY
#     and Hindsight keys — secrets in `.env`, never in Nix config.
#   * Firewall keeps the port tailnet-only (`tailscale0`), so even though
#     the server binds 0.0.0.0 the only peers who can reach it are on the
#     tailnet. Same pattern as hindsight (:8888) and the HA proxy (:8444).
#     The tailnet port itself is opened by the exposure registry
#     (modules/exposure.nix), which also fails the build if this service is
#     exposed without credentials.
#
# The systemd unit is deliberately named `hermes-dashboard.service` — that
# is the canonical name `hermes update` uses to restart a managed dashboard
# instead of raw-killing the PID.

{
  config,
  pkgs,
  lib,
  ...
}:

let
  cfg = config.services.hermes-dashboard;
  agent = config.services.hermes-agent;
in
{
  options.services.hermes-dashboard = {
    enable = lib.mkEnableOption "Hermes web dashboard (hermes dashboard)";

    port = lib.mkOption {
      type = lib.types.int;
      default = 9119;
      description = "Port the dashboard binds (default hermes dashboard port).";
    };

    bind = lib.mkOption {
      type = lib.types.str;
      default = "0.0.0.0";
      description = ''
        Address the dashboard binds. Non-loopback engages the dashboard auth
        gate (a public bind always requires an auth provider). The NixOS
        firewall restricts the port to the tailscale interface, so binding
        0.0.0.0 is still tailnet-only.
      '';
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        Agenix-decrypted env file with dashboard auth credentials:
          HERMES_DASHBOARD_BASIC_AUTH_USERNAME
          HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH   (preferred) or _PASSWORD
          HERMES_DASHBOARD_BASIC_AUTH_SECRET          (token-signing key)
        It is appended to the default profile's .env (via
        services.hermes-agent.environmentFiles) so the dashboard process
        reads the credentials at startup from $HERMES_HOME/.env.
      '';
    };

    extraArgs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Extra command-line arguments for `hermes dashboard`.";
    };
  };

  config = lib.mkIf cfg.enable {
    # Credentials flow into $HERMES_HOME/.env, the single source of truth
    # the dashboard reads at startup (load_hermes_dotenv). Same mechanism
    # as the api_server key and provider keys.
    services.hermes-agent.environmentFiles = lib.optionals (cfg.environmentFile != null) [
      cfg.environmentFile
    ];

    # Tailnet exposure: the registry opens :${port} on tailscale0 AND fails
    # the build if no dashboard credentials are wired. The dashboard binds
    # 0.0.0.0, so without the auth gate this would be an unauthenticated
    # admin panel on the tailnet.
    hermesDeploy.exposure.services = [
      {
        name = "hermes-dashboard";
        port = cfg.port;
        auth = {
          type = "basic";
          credentialsConfigured = cfg.environmentFile != null;
        };
      }
    ];

    systemd.services.hermes-dashboard = {
      description = "Hermes web dashboard";
      wantedBy = [ "multi-user.target" ];
      after = [
        "hermes-agent.service"
        "network-online.target"
      ];
      wants = [
        "hermes-agent.service"
        "network-online.target"
      ];

      environment = {
        HOME = agent.stateDir;
        HERMES_HOME = "${agent.stateDir}/.hermes";
        HERMES_MANAGED = "true";
      };

      serviceConfig = {
        User = agent.user;
        Group = agent.group;
        WorkingDirectory = agent.workingDirectory;

        ExecStart = lib.concatStringsSep " " (
          [
            "${agent.package}/bin/hermes"
            "dashboard"
            "--host"
            cfg.bind
            "--port"
            (toString cfg.port)
            "--no-open"
          ]
          ++ cfg.extraArgs
        );

        Restart = "always";
        RestartSec = 5;

        # ── The bot is root — the dashboard lane must not veto it ────────
        # hosts/hermes/configuration.nix gives hermes passwordless sudo for
        # ALL commands ("bot is root"); kanban workers are spawned by this
        # dashboard unit, so NoNewPrivileges here would kill sudo/setuid at
        # the kernel level in every worker session. Relax both exactly like
        # the gateway lane (hermes-service.nix) — the safety net is the
        # rollback machinery, not a kernel sandbox. ProtectSystem=strict is
        # NOT a usable middle ground: it per-mount-locks / ro in the unit
        # namespace and even root inside the namespace cannot remount it rw
        # (mount: permission denied, EPERM), so workers could never write
        # the flake or run nixos-rebuild.
        UMask = "0007";
        NoNewPrivileges = lib.mkForce false;
        ProtectSystem = lib.mkForce false;
        ProtectHome = false;
        ReadWritePaths = [
          agent.stateDir
          agent.workingDirectory
        ];
        PrivateTmp = true;
      };

      # The base PATH only has the agent's extraPackages. Expose the setuid
      # sudo wrapper (/run/wrappers/bin) and the system tools
      # (/run/current-system/sw/bin) so kanban workers spawned from this
      # unit can actually sudo — same as the gateway lane (hermes-service.nix).
      path = with pkgs; [
        bash
        coreutils
        git
        "/run/wrappers"
        "/run/current-system/sw"
      ];
    };
  };
}
