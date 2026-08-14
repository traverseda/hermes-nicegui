# Fast, build-light verification of the tailnet-exposure guardrails.
#
# The exposure registry (modules/exposure.nix) is the single place that
# opens ports on the tailnet, and it FAILS THE BUILD when a service is
# exposed without credentials. This check evals the module in isolation
# (lib.evalModules) and asserts on the generated `assertions` at eval time:
#
#   * a well-credentialed exposure builds clean and the firewall port is
#     derived from the registry,
#   * a credentialed service exposed without its credentials wired trips
#     the "credentialsConfigured" assertion,
#   * an unauthenticated (auth.type = "none") service without the double
#     opt-in trips the "allowInsecure" assertion,
#   * a port opened on tailscale0 that is NOT in the registry trips the
#     "undeclared" assertion.
#
# Run with:  nix build .#checks.x86_64-linux.exposure-config-check

{ nixpkgs }:
let
  system = "x86_64-linux";
  inherit (nixpkgs) lib;
  pkgs = nixpkgs.legacyPackages.${system};

  # Stub options the exposure module touches, so we can eval it standalone
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
  };

  evalExposure =
    exposureServices: extraFirewallPorts:
    (lib.evalModules {
      modules = [
        ../modules/exposure.nix
        stubOptions
        {
          hermesDeploy.exposure.services = exposureServices;
          networking.firewall.interfaces.tailscale0.allowedTCPPorts = extraFirewallPorts;
        }
      ];
    }).config;

  failing = cfg: lib.filter (a: !a.assertion) cfg.assertions;
  messages = cfg: builtins.concatStringsSep "\n---\n" (map (a: a.message) (failing cfg));

  # 0 — everything properly credentialed and declared: should build clean.
  ok =
    evalExposure
      [
        {
          name = "example";
          port = 9000;
          auth = {
            type = "bearer";
            credentialsConfigured = true;
          };
        }
      ]
      [ ];

  # 1 — credentialed but no credentials wired.
  missingCreds =
    evalExposure
      [
        {
          name = "example";
          port = 9000;
          auth = {
            type = "bearer";
            credentialsConfigured = false;
          };
        }
      ]
      [ ];

  # 2 — unauthenticated without the double opt-in.
  insecureNoOptIn =
    evalExposure
      [
        {
          name = "example";
          port = 9000;
          auth = {
            type = "none";
          };
        }
      ]
      [ ];

  # 3 — a port opened on the tailnet outside the registry.
  undeclared = evalExposure [ ] [ 9100 ];
in
pkgs.runCommand "exposure-config-check"
  {
    okClean = if (failing ok) == [ ] then "yes" else "no";
    okFirewall = builtins.concatStringsSep "," (
      map toString ok.networking.firewall.interfaces.tailscale0.allowedTCPPorts
    );
    missingCredsMsgs = messages missingCreds;
    insecureMsgs = messages insecureNoOptIn;
    undeclaredMsgs = messages undeclared;
  }
  ''
    # 0 — valid exposure: no failing assertions, firewall derived from registry.
    test "$okClean" = "yes" || { echo "UNEXPECTED: clean config failed assertions" >&2; exit 1; }
    echo "$okFirewall" | grep -q "9000" || { echo "MISSING: registry did not open :9000 (got $okFirewall)" >&2; exit 1; }

    # 1 — missing credentials must fail with an actionable message.
    echo "$missingCredsMsgs" | grep -qi "credentialsConfigured" \
      || { echo "MISSING: credentialsConfigured assertion" >&2; exit 1; }
    echo "$missingCredsMsgs" | grep -qi "example" \
      || { echo "MISSING: credential assertion names the service" >&2; exit 1; }

    # 2 — auth.type = "none" without the double opt-in must fail.
    echo "$insecureMsgs" | grep -qi "allowInsecure" \
      || { echo "MISSING: allowInsecure assertion" >&2; exit 1; }

    # 3 — an undeclared tailnet port must fail.
    echo "$undeclaredMsgs" | grep -qi "not declared" \
      || { echo "MISSING: undeclared-port assertion" >&2; exit 1; }

    touch "$out"
    echo "exposure-config-check passed"
  ''
