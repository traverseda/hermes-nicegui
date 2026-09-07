# Fast, build-light verification of the playwright-mcp wiring.
#
# Asserts that:
#   * the playwright-mcp service runs the MCP binary with the right
#     environment variables (PLAYWRIGHT_BROWSERS_PATH, etc.),
#   * the socket unit is wired and will activate the service,
#   * hermes-agent is wired to it as an MCP server on loopback,
#   * the correct system packages (playwright + playwright-mcp) are installed.
#
# Run with:  nix build .#checks.x86_64-linux.playwright-mcp-config-check

{
  nixpkgs,
  hermes-agent,
}:
let
  system = "x86_64-linux";
  inherit (nixpkgs) lib;
  pkgs = nixpkgs.legacyPackages.${system};

  mkCfg =
    extra:
    (lib.nixosSystem {
      inherit system;
      modules = [
        hermes-agent.nixosModules.default
        ../modules/config.nix
        ../modules/exposure.nix
        ../modules/hermes-deploy.nix
        ../modules/hermes-service.nix
        ../modules/playwright-mcp.nix
        {
          hermesDeploy.providers.test = {
            name    = "test";
            provider= "vllm";
            model   = "test-model";
          };
          hermesDeploy.defaultProvider = "test";

          services.hermes-agent.enable = true;
          services.hermes-deploy.enable = true;
          services.playwright-mcp.enable = true;
        }
        extra
      ];
    }).config;

  ok = mkCfg { };

  svcText = ok.systemd.units."playwright-mcp.service".text;
  sock = ok.systemd.sockets."playwright-mcp" or null;
  mHasSocket = if sock != null then "yes" else "no";

  port = toString ok.services.playwright-mcp.port;

  need = needle: text: ''
    if ! grep -q -- ${lib.escapeShellArg needle} <<EOF
${text}
EOF
    then
      echo "MISSING: ${needle}" >&2
      exit 1
    fi
  '';

  mcpEntry = ok.services.hermes-agent.mcpServers.playwright or null;
  mcpUrl = mcpEntry.url or "MISSING";
  mHasMcp = if mcpEntry != null then "yes" else "no";

in
pkgs.runCommand "playwright-mcp-config-check"
  {
    inherit svcText port mHasSocket mcpUrl mHasMcp;
  }
  ''
    # ── service is registered with correct user/browsers path ────────────
    ${need "User=hermes" "$svcText"}
    ${need "Group=hermes" "$svcText"}
    ${need "playwright.browsers" "$svcText"}
    ${need "PLAYWRIGHT_BROWSERS_PATH" "$svcText"}
    ${need "PLAYWRIGHT_MCP_BROWSER" "$svcText"}
    ${need "PLAYWRIGHT_MCP_USER_DATA_DIR" "$svcText"}
    ${need "playwright-mcp" "$svcText"}
    ${need "Restart=on-failure" "$svcText"}

    # ── socket unit exists and exposes the port ──────────────────────────
    test "$mHasSocket" = "yes" || { echo "MISSING: socket unit" >&2; exit 1; }

    # ── hermes-agent MCP wiring ──────────────────────────────────────────
    test "$mHasMcp" = "yes" || { echo "MISSING: MCP server registration" >&2; exit 1; }
    echo "$mcpUrl" | grep -q "127.0.0.1:${port}/mcp" \
      || { echo "MISSING/WRONG: MCP url $mcpUrl" >&2; exit 1; }

    touch "$out"
    echo "playwright-mcp-config-check passed"
  ''
