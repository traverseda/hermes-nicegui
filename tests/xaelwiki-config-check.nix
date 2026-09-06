# Fast, build-light verification of the xaelwiki wiring.
#
# Asserts that:
#   * the xaelwiki service runs the vendored source as an IMMUTABLE store path
#     (PYTHONPATH points into /nix/store, not a live checkout) via the Nix
#     python env,
#   * the notes vault bootstrap installs the deploy key + clones the vault,
#   * the exposure registry entry exists and is credential-enforced,
#   * hermes-agent is wired to it as an MCP server with an env-interpolated
#     bearer token (never the literal secret in config),
#   * a deployment with the token but no ssh key still fails the exposure
#     assertion (missing credentials = no build).
#
# Run with:  nix build .#checks.x86_64-linux.xaelwiki-config-check

{
  nixpkgs,
  hermes-agent,
  xaelwiki-src,
}:
let
  system = "x86_64-linux";
  inherit (nixpkgs) lib;
  pkgs = nixpkgs.legacyPackages.${system};

  mkCfg =
    extra: disableSecrets:
    (lib.nixosSystem {
      inherit system;
      specialArgs = { inherit xaelwiki-src; };
      modules = [
        hermes-agent.nixosModules.default
        ../modules/config.nix
        ../modules/exposure.nix
        ../modules/hermes-deploy.nix
        ../modules/hermes-service.nix
        ../modules/xaelwiki.nix
        {
          hermesDeploy.providers.test = {
            name    = "test";
            provider= "vllm";
            model   = "test-model";
          };
          hermesDeploy.defaultProvider = "test";

          services.hermes-agent.enable = true;
          services.hermes-deploy.enable = true;
          services.xaelwiki = {
            enable = true;
            environmentFile = if disableSecrets then null else "/dummy/xaelwiki-env";
            sshKeyFile = if disableSecrets then null else "/dummy/xaelwiki-ssh";
          };
        }
        extra
      ];
    }).config;

  ok = mkCfg { } false;
  missingCreds = mkCfg { } true;

  xaelUnit = pkgs.writeText "xaelwiki.unit" cfgText;
  vaultUnit = pkgs.writeText "xaelwiki-vault.unit" cfgVaultText;
  vaultScript = pkgs.writeText "xaelwiki-vault.script" cfgVaultScript;

  # Extract unit/script text for the two services.
  cfgText = ok.systemd.units."xaelwiki.service".text;
  cfgVaultText = ok.systemd.units."xaelwiki-vault.service".text;
  cfgVaultScript = ok.systemd.services."xaelwiki-vault".script;

  exposureEntry = builtins.head (
    builtins.filter (e: e.name == "xaelwiki") ok.hermesDeploy.exposure.services
  );
  mcpEntry = ok.services.hermes-agent.mcpServers.xaelwiki or { };

  exposureMsgs =
    cfg:
    builtins.concatStringsSep "\n---\n" (
      map (a: a.message) (
        lib.filter (a: !a.assertion && lib.hasPrefix "hermesDeploy.exposure" a.message) cfg.assertions
      )
    );

  need = needle: file: ''
    if ! grep -q -- ${lib.escapeShellArg needle} ${file}; then
      echo "MISSING: ${needle} in ${file}" >&2
      exit 1
    fi
  '';

  mustNot = needle: file: ''
    if grep -q -- ${lib.escapeShellArg needle} ${file}; then
      echo "UNEXPECTED: ${needle} in ${file}" >&2
      exit 1
    fi
  '';
in
pkgs.runCommand "xaelwiki-config-check"
  {
    inherit xaelUnit vaultUnit vaultScript;
    exposurePort = toString exposureEntry.port;
    exposureAuth = exposureEntry.auth.type;
    mcpUrl = mcpEntry.url or "MISSING";
    mcpHeader = mcpEntry.headers.Authorization or "MISSING";
    missingCredsMsgs = exposureMsgs missingCreds;
  }
  ''
    cat $xaelUnit > xaelwiki.unit
    cat $vaultUnit > vault.unit
    cat $vaultScript > vault.script

    # ── service runs the vendored source as an immutable store path ─────
    ${need "-m xaelwiki.server" "xaelwiki.unit"}
    ${need "PYTHONPATH=/nix/store/" "xaelwiki.unit"}
    ${mustNot "/var/lib/xaelwiki/src" "xaelwiki.unit"}
    ${need "User=xaelwiki" "xaelwiki.unit"}
    ${need "ReadWritePaths=/var/lib/xaelwiki" "xaelwiki.unit"}
    ${need "XAEL_READ_ONLY=0" "xaelwiki.unit"}            # notes are editable by default (not the server code)

    # ── vault bootstrap installs the deploy key + clones the notes ─────
    ${need "Type=oneshot" "vault.unit"}
    ${need "User=xaelwiki" "vault.unit"}
    ${need "known_hosts" "vault.script"}
    ${need "codeberg.org" "vault.script"}
    ${need "id_ed25519" "vault.script"}

    # ── exposure registry: xaelwiki registered, bearer-auth enforced ───
    test "$exposurePort" = "8000" || { echo "UNEXPECTED: exposure port $exposurePort" >&2; exit 1; }
    test "$exposureAuth" = "bearer" || { echo "UNEXPECTED: exposure auth $exposureAuth" >&2; exit 1; }

    # ── hermes MCP wiring uses an env placeholder, never the literal ────
    echo "$mcpUrl" | grep -q "127.0.0.1:8000/mcp" \
      || { echo "MISSING: MCP url $mcpUrl" >&2; exit 1; }
    echo "$mcpHeader" | grep -q 'Bearer ''${XAEL_AUTH_TOKEN}' \
      || { echo "MISSING: MCP header is not an env placeholder: $mcpHeader" >&2; exit 1; }

    # ── missing credentials → exposure registry fails the build ─────────
    echo "$missingCredsMsgs" | grep -qi "credentialsConfigured" \
      || { echo "MISSING: exposure credential assertion" >&2; exit 1; }
    echo "$missingCredsMsgs" | grep -qi "xaelwiki" \
      || { echo "MISSING: exposure assertion names xaelwiki" >&2; exit 1; }

    touch "$out"
    echo "xaelwiki-config-check passed"
  ''
