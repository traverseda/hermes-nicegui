# playwright-mcp — browser automation MCP server for the Hermes agent.
#
# playwright-mcp exposes browser automation tools (navigate, click, fill,
# screenshot, etc.) over the MCP protocol. Running it as a dedicated systemd
# service keeps Playwright's browser resources isolated from the hermes-agent
# process and provides stable lifecycle management across health checks.
#
# Design:
#   * SYSTEMD SERVICE + SOCKET: the playwright-mcp process runs as a
#     dedicated service with socket activation so it is always reachable on
#     the MCP endpoint. State (cookies, cached browsers) lives in a
#     RuntimeDirectory that is wiped on stop — no leakage between deploys.
#   * LOOPBACK ONLY: binds 127.0.0.1 so only local consumers (hermes-agent)
#     can reach it. No firewall rule needed.
#   * HERMES USER: runs as `hermes` (the same user as hermes-agent) so
#     browser binaries and state are accessible without extra permissions.

{
  config,
  pkgs,
  lib,
  ...
}:

let
  cfg = config.services.playwright-mcp;
  agent = config.services.hermes-agent;

  # Persistent browser data: cookies, cache, localStorage.
  # Wiped on stop (RuntimeDirectory) so each deploy starts clean.
  runtimeStatePath = "/run/playwright-mcp";

in
{
  options.services.playwright-mcp = {
    enable = lib.mkEnableOption "playwright-mcp browser automation MCP server";

    port = lib.mkOption {
      type = lib.types.port;
      default = 9213;
      description = "Port the MCP HTTP endpoint listens on (loopback only).";
    };
  };

  config = lib.mkIf cfg.enable {
    # ── MCP server registration ──────────────────────────────────────────
    services.hermes-agent.mcpServers.playwright = {
      url = "http://127.0.0.1:${toString cfg.port}/mcp";
    };

    # ── System packages: Playwright browsers + MCP binary ────────────────
    environment.systemPackages = [
      pkgs.playwright          # browser binaries (chromium, firefox, webkit)
      pkgs.playwright-mcp      # MCP server binary
    ];

    # ── Socket activation ────────────────────────────────────────────────
    # systemd.sockets.<name> auto-activates systemd.services.<name>.
    systemd.sockets.playwright-mcp = {
      description = "playwright-mcp MCP socket";
      socketConfig.ListenStream = cfg.port;
      wantedBy = [ "sockets.target" ];
    };

    # ── The MCP service ──────────────────────────────────────────────────
    systemd.services.playwright-mcp = {
      description = "Playwright MCP browser automation server";

      # Must start after the socket is ready.
      requires = [ "playwright-mcp.socket" ];
      after = [ "playwright-mcp.socket" "hermes-agent.service" ];

      serviceConfig = {
        User = agent.user;
        Group = agent.group;

        # Browser cookies, cache, and profiles — wiped on stop.
        RuntimeDirectory = "playwright-mcp";

        # Where to find Playwright-installed browsers.
        Environment = [
          "PLAYWRIGHT_BROWSERS_PATH=${pkgs.playwright.browsers}"
          "PLAYWRIGHT_MCP_BROWSER=firefox"
          "PLAYWRIGHT_MCP_USER_DATA_DIR=${runtimeStatePath}/firefox-profile"
        ];

        ExecStart = "${pkgs.playwright-mcp}/bin/playwright-mcp";

        Restart = "on-failure";
        RestartSec = 5;

        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        UMask = "0007";
      };
    };
  };
}
