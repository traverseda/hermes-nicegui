# Central tailnet-exposure registry.
#
# Every port this box intentionally opens on the tailnet (tailscale0) MUST
# be declared here as an exposure, and every declared exposure MUST prove
# how it authenticates. This makes "open a port on the tailnet" and "have
# credentials for it" a single atomic declaration — you cannot add a
# service port without stating its auth, and you cannot add credentials
# later and forget to re-check the exposure.
#
# Enforcement (all fail the BUILD, not warn):
#   1. The firewall rule is DERIVED from this registry. Service modules no
#      longer touch `networking.firewall.interfaces.tailscale0` directly,
#      so an open port and a credentialed service can't drift apart.
#   2. A credentialed auth type (`bearer`/`basic`/`oauth`) requires
#      `auth.credentialsConfigured = true` — i.e. the agenix secret / env
#      file is actually wired before the port is opened.
#   3. An unauthenticated exposure (`auth.type = "none"`) requires a
#      deliberate double opt-in: `auth.insecure = true` on the service AND
#      `hermesDeploy.exposure.allowInsecure = true` globally.
#   4. Any port opened on tailscale0 that is NOT declared here fails the
#      build, so a host config (or a future module) cannot silently open an
#      extra port outside the registry.

{
  config,
  lib,
  ...
}:

let
  cfg = config.hermesDeploy.exposure;
  services = cfg.services;

  exposurePorts = map (s: s.port) services;

  # ── Violations (for precise assertion messages) ─────────────────────
  insecureViolations = lib.filter (
    s: s.auth.type == "none" && !(cfg.allowInsecure && s.auth.insecure)
  ) services;

  missingCreds = lib.filter (s: s.auth.type != "none" && !s.auth.credentialsConfigured) services;

  undeclaredPorts = lib.subtractLists exposurePorts config.networking.firewall.interfaces.tailscale0.allowedTCPPorts;

  namesOf = xs: lib.concatStringsSep ", " (map (s: "${s.name} (:${toString s.port})") xs);
in
{
  options.hermesDeploy.exposure = {
    allowInsecure = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Global gate for exposing services WITHOUT credentials. This is a
        deliberate, repo-wide decision that the tailnet is trusted enough to
        skip auth on services that individually opt in via `auth.insecure`.
        It must be true (and each service must set `auth.insecure = true`)
        for any `auth.type = "none"` exposure to build.
      '';
    };

    services = lib.mkOption {
      type = lib.types.listOf (
        lib.types.submodule {
          options = {
            name = lib.mkOption {
              type = lib.types.str;
              description = "Human-readable service name, used in assertion messages.";
            };

            port = lib.mkOption {
              type = lib.types.port;
              description = "TCP port opened on the tailnet interface.";
            };

            auth = lib.mkOption {
              type = lib.types.submodule {
                options = {
                  type = lib.mkOption {
                    type = lib.types.enum [
                      "bearer"
                      "basic"
                      "oauth"
                      "none"
                    ];
                    description = "How this service authenticates its callers.";
                  };

                  credentialsConfigured = lib.mkOption {
                    type = lib.types.bool;
                    default = false;
                    description = ''
                      Whether the credential source (agenix secret / env file)
                      is actually wired for this service. Build fails if a
                      credentialed service is exposed with this false.
                    '';
                  };

                  insecure = lib.mkOption {
                    type = lib.types.bool;
                    default = false;
                    description = ''
                      Conscious per-service opt-in for `auth.type = "none"`.
                      Only honored when `hermesDeploy.exposure.allowInsecure`
                      is also true.
                    '';
                  };
                };
              };
            };
          };
        }
      );
      default = [ ];
      description = "Tailnet-exposed services and how they authenticate.";
    };
  };

  config = {
    # Single source of truth: only the registry opens tailnet ports.
    # Any port on this list from elsewhere trips the undeclared assertion.
    networking.firewall.interfaces.tailscale0.allowedTCPPorts = exposurePorts;

    assertions = [
      {
        # Unauthenticated exposures require a double opt-in.
        assertion = insecureViolations == [ ];
        message = ''
          hermesDeploy.exposure: these services declare auth.type = "none"
          (no credentials) without the full opt-in: ${namesOf insecureViolations}.
          Exposing a service unauthenticated requires BOTH auth.insecure = true
          on the service AND hermesDeploy.exposure.allowInsecure = true.
        '';
      }
      {
        # Credentialed exposures must actually have credentials wired.
        assertion = missingCreds == [ ];
        message = ''
          hermesDeploy.exposure: these services are exposed with
          auth.type != "none" but credentialsConfigured = false:
          ${namesOf missingCreds}.
          Wire their credential source (agenix environmentFile / secret) before
          exposing them on the tailnet, or they will not build.
        '';
      }
      {
        # Nothing opens a tailnet port outside the registry.
        assertion = undeclaredPorts == [ ];
        message = ''
          hermesDeploy.exposure: these ports are open on the tailnet interface
          but are not declared in hermesDeploy.exposure.services:
          ${toString undeclaredPorts}.
          Declare each with an auth block (and credentials), or the build will
          fail — every tailnet port must be an accounted-for, credentialed
          exposure.
        '';
      }
    ];
  };
}
