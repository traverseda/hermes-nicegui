# Cloudflare Tunnel (remotely-managed, outbound-only).
#
# Runs `cloudflared tunnel run --token` against a DASHBOARD-MANAGED tunnel:
# the tunnel and its public hostnames are created/configured in the Cloudflare
# dashboard (Zero Trust → Networks → Tunnels), and cloudflared on the LXC just
# dials out to Cloudflare with the tunnel's connector token. No inbound port is
# opened on the LXC.
#
# Security model:
#   * The connector token (cfut_…) comes from an agenix secret
#     (`cloudflare-tunnel`), never from Nix config, and is handed to the unit
#     via systemd LoadCredential — systemd mounts it into
#     $CREDENTIALS_DIRECTORY/token with 0600 perms for the DynamicUser, so the
#     literal token never reaches /nix/store or the flake repo.
#   * Outbound-only: cloudflared only establishes outbound connections; no
#     firewall rule is required or opened, and the tailnet-exposure registry
#     (modules/exposure.nix) is untouched.
#   * INGRESS LIVES IN THE DASHBOARD. Unlike a locally-managed tunnel there is
#     no local ingress table here, so the exposure-registry credential
#     guardrails do NOT apply to the public path — the hostnames Cloudflare
#     routes to this LXC are whatever the dashboard says. Publishing a hostname
#     is a dashboard action, not a flake edit. (This is the deliberate trade-off
#     of remotely-managed mode; the old locally-managed module is in git
#     history.)
#   * The unit is hardened: DynamicUser, NoNewPrivileges, ProtectSystem=strict,
#     PrivateTmp.
#
# The connector token is rotated by the operator in the Cloudflare dashboard;
# re-encrypt the new value into secrets/cloudflare-tunnel.age and redeploy.

{
  config,
  pkgs,
  lib,
  ...
}:

let
  cfg = config.services.cloudflare-tunnel;

  runScript = pkgs.writeShellScript "cloudflared-tunnel-run" ''
    set -euo pipefail
    # systemd LoadCredential mounts the agenix token here (0600, root-owned
    # runtime dir); the shell strips the trailing newline.
    exec ${cfg.package}/bin/cloudflared tunnel --no-autoupdate run \
      --token "$(cat "$CREDENTIALS_DIRECTORY/token")"
  '';
in
{
  options.services.cloudflare-tunnel = {
    enable = lib.mkEnableOption "Cloudflare Tunnel (remotely-managed, outbound-only)";

    tokenFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        Agenix-decrypted file containing the tunnel connector token
        (`cfut_…`, from `cloudflared tunnel token <name>` or the dashboard).
        systemd LoadCredential exposes it to the unit as
        $CREDENTIALS_DIRECTORY/token — never in Nix config or the store.
      '';
    };

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.cloudflared;
      description = "cloudflared package to run the tunnel with.";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.services.cloudflared-tunnel = {
      description = "Cloudflare Tunnel (remotely-managed, outbound-only)";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];

      path = [ cfg.package ];

      serviceConfig = {
        LoadCredential = lib.optional (cfg.tokenFile != null) "token:${cfg.tokenFile}";
        ExecStart = runScript;
        DynamicUser = true;
        Restart = "on-failure";
        RestartSec = 5;
        UMask = "0007";
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
      };
    };

    warnings = lib.optionals (cfg.tokenFile == null) [
      "services.cloudflare-tunnel: no tokenFile set — the tunnel cannot connect until the connector token (cfut_…) is supplied via the agenix secret."
    ];
  };
}
