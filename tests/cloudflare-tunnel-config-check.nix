# Fast, build-light verification of the Cloudflare Tunnel wiring + its
# credential enforcement.
#
# The tunnel's ingress rules must reference exposures registered in
# hermesDeploy.exposure.services, and only credentialed ones may be exposed
# publicly. This check evals the module (with the exposure registry) and
# asserts:
#
#   * a valid credentialed exposure referenced by name derives the right
#     local URL and produces no failing assertions,
#   * an unknown exposure name in ingress trips an assertion,
#   * an uncredentialed (credentialsConfigured = false) exposure in ingress
#     trips an assertion,
#   * an unauthenticated (auth.type = "none") exposure in ingress trips an
#     assertion,
#   * empty ingress + http_status:404 default keeps nothing public.
#
# Run with:  nix build .#checks.x86_64-linux.cloudflare-tunnel-config-check

{ nixpkgs }:
let
  system = "x86_64-linux";
  inherit (nixpkgs) lib;
  pkgs = nixpkgs.legacyPackages.${system};

  tunnelId = "11111111-2222-3333-4444-555555555555";

  # Stub options the two modules touch, so we can eval them standalone
  # without booting the whole NixOS system.
  stubOptions = {
    options.networking.firewall.interfaces.tailscale0.allowedTCPPorts = lib.mkOption {
      type = lib.types.listOf lib.types.int;
      default = [ ];
    };
    options.assertions = lib.mkOption {
      type = lib.types.listOf (
        lib.types.submodule {
          options = {
            assertion = lib.mkOption { type = lib.types.bool; };
            message = lib.mkOption { type = lib.types.str; };
          };
        }
      );
      default = [ ];
    };
    options.warnings = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
    };
    options.services.cloudflared.enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
    };
    options.services.cloudflared.tunnels = lib.mkOption {
      type = lib.types.attrsOf lib.types.attrs;
      default = { };
    };
  };

  # The registry entries a real deployment would register.
  credExposure = {
    name = "hermes-dashboard";
    port = 9119;
    auth = {
      type = "basic";
      credentialsConfigured = true;
    };
  };
  uncredExposure = credExposure // {
    name = "uncredentialed";
    auth = {
      type = "basic";
      credentialsConfigured = false;
    };
  };
  insecureExposure = {
    name = "noauth";
    port = 9001;
    auth = {
      type = "none";
    };
  };

  mkCfg =
    registry: ingress:
    (lib.evalModules {
      modules = [
        ../modules/exposure.nix
        ../modules/cloudflare-tunnel.nix
        stubOptions
        {
          hermesDeploy.exposure.services = registry;
          services.cloudflare-tunnel = {
            enable = true;
            inherit tunnelId;
            credentialsFile = "/dummy/credentials.json";
            inherit ingress;
          };
        }
      ];
    }).config;

  # The generated cloudflared config JSON for a given config.
  configJson = cfg: builtins.toJSON cfg.services.cloudflared.tunnels.${tunnelId};

  tunnelAssertionMsgs =
    cfg:
    builtins.concatStringsSep "\n---\n" (
      map (a: a.message) (lib.filter (a: !a.assertion) cfg.assertions)
    );

  ok = mkCfg [ credExposure ] { "dashboard.0u0.ca" = "hermes-dashboard"; };
  unknown = mkCfg [ credExposure ] { "x.0u0.ca" = "does-not-exist"; };
  uncred = mkCfg [ uncredExposure ] { "x.0u0.ca" = "uncredentialed"; };
  insecure = mkCfg [ insecureExposure ] { "x.0u0.ca" = "noauth"; };

  need = needle: file: ''
    if ! grep -q -- ${lib.escapeShellArg needle} ${file}; then
      echo "MISSING: ${needle} in ${file}" >&2
      exit 1
    fi
  '';
in
pkgs.runCommand "cloudflare-tunnel-config-check"
  {
    okConfig = pkgs.writeText "ok-config.json" (configJson ok);
    okMsgs = tunnelAssertionMsgs ok;
    unknownMsgs = tunnelAssertionMsgs unknown;
    uncredMsgs = tunnelAssertionMsgs uncred;
    insecureMsgs = tunnelAssertionMsgs insecure;
  }
  ''
    # ── valid credentialed exposure referenced by name ─────────────────
    cat $okConfig > ok.config
    ${need "http://127.0.0.1:9119" "ok.config"}      # URL derived from registry port
    ${need "dashboard.0u0.ca" "ok.config"}
    test -z "$okMsgs" || { echo "UNEXPECTED: valid config failed: $okMsgs" >&2; exit 1; }

    # ── empty ingress → nothing public (404 catch-all) ─────────────────
    ${need "http_status:404" "ok.config"}

    # ── unknown exposure name → build fails ────────────────────────────
    echo "$unknownMsgs" | grep -qi "references exposure(s)" \
      || { echo "MISSING: unknown-exposure assertion" >&2; exit 1; }
    echo "$unknownMsgs" | grep -qi "does-not-exist" \
      || { echo "MISSING: unknown-exposure assertion names the target" >&2; exit 1; }

    # ── uncredentialed exposure → build fails ──────────────────────────
    echo "$uncredMsgs" | grep -qi "credentialsConfigured" \
      || { echo "MISSING: uncredentialed assertion" >&2; exit 1; }
    echo "$uncredMsgs" | grep -qi "uncredentialed" \
      || { echo "MISSING: uncredentialed assertion names the target" >&2; exit 1; }

    # ── auth.type = "none" on the public internet → build fails ────────
    echo "$insecureMsgs" | grep -qi "PUBLIC internet" \
      || { echo "MISSING: unauthenticated assertion" >&2; exit 1; }
    echo "$insecureMsgs" | grep -qi "noauth" \
      || { echo "MISSING: unauthenticated assertion names the target" >&2; exit 1; }

    touch "$out"
    echo "cloudflare-tunnel-config-check passed"
  ''
