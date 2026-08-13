# Standalone Hindsight memory service (https://github.com/vectorize-io/hindsight).
#
# Hindsight gives AI agents persistent memory (retain / recall / reflect) backed
# by PostgreSQL with pgvector. We run the officially-shipped container image
# under rootful podman (NixOS `virtualisation.oci-containers`), because its
# Python dependency surface (litellm, obstore, claude-agent-sdk, markitdown, …)
# is not feasible to package natively in nixpkgs.
#
# Runtime decisions:
#   * `network = "host"`  — the container shares the host network namespace, so
#     the API binds directly on the host and the NixOS firewall (INPUT chain)
#     is what actually gates access. We expose the API only on the tailscale
#     interface → tailnet-only; everything else stays firewalled off.
#   * Embedded PostgreSQL (`pg0`)  — the image bundles its own PostgreSQL 15 +
#     pgvector. Data lives in ~/.pg0 inside the container, which we bind-mount
#     to ${stateDir} and chown to the container's uid (1000).
#   * Image is pinned by digest  — updating = deliberate flake edit → new NixOS
#     generation → rollback-safe, same as any other change here.
#   * Secrets (LLM key, tenant API key, control-plane key) come from an agenix
#     env file, never from Nix config.
#
# Clients reach the API over the tailnet:  http://<tailscale-ip>:8888  with
# `Authorization: Bearer $HINDSIGHT_API_TENANT_API_KEY`. Hermes (same host)
# uses http://127.0.0.1:8888.

{
  config,
  lib,
  ...
}:

let
  cfg = config.services.hindsight;
  llm = config.hermesDeploy.llm;
in
{
  options.services.hindsight = {
    enable = lib.mkEnableOption "Hindsight memory service";

    image = lib.mkOption {
      type = lib.types.str;
      default = "ghcr.io/vectorize-io/hindsight@sha256:6364c3c5f1e551447976d6c3ab369040d0237c0980f10f911d76d981290913b6";
      description = ''
        OCI image to run, pinned by digest. Bump deliberately with:
          skopeo inspect docker://ghcr.io/vectorize-io/hindsight:latest
      '';
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/hindsight";
      description = "Host directory holding Hindsight's embedded database (.pg0).";
    };

    # The image runs as the unprivileged `hindsight` user (UID 1000).
    uid = lib.mkOption {
      type = lib.types.int;
      default = 1000;
    };

    gid = lib.mkOption {
      type = lib.types.int;
      default = 1000;
    };

    apiPort = lib.mkOption {
      type = lib.types.int;
      default = 8888;
      description = "Hindsight REST API port.";
    };

    enableControlPlane = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Run the optional Next.js control-plane dashboard (:9999). Most tools
        only need the API; leave off unless you want the web UI.
      '';
    };

    controlPlanePort = lib.mkOption {
      type = lib.types.int;
      default = 9999;
    };

    enableApiKeyAuth = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Require an API key on every request
        (HINDSIGHT_API_TENANT_EXTENSION=ApiKeyTenantExtension). The key itself
        must be HINDSIGHT_API_TENANT_API_KEY in the environmentFile.
      '';
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        Agenix-decrypted env file with secrets. Expected variables:
          HINDSIGHT_API_LLM_API_KEY   — key for the preferred model endpoint
          HINDSIGHT_API_TENANT_API_KEY — shared key clients send as Bearer
          HINDSIGHT_CP_ACCESS_KEY     — control-plane login (only w/ enableControlPlane)
      '';
    };

    autoStart = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Start the container automatically on boot.";
    };
  };

  config = lib.mkIf cfg.enable {
    virtualisation.oci-containers = {
      backend = "podman";
      containers.hindsight = {
        image = cfg.image;
        networks = [ "host" ];
        user = "${toString cfg.uid}:${toString cfg.gid}";
        autoStart = cfg.autoStart;
        environmentFiles = lib.optional (cfg.environmentFile != null) cfg.environmentFile;
        environment = {
          HINDSIGHT_API_HOST = "0.0.0.0";
          HINDSIGHT_API_PORT = toString cfg.apiPort;
          HINDSIGHT_ENABLE_API = "true";
          HINDSIGHT_ENABLE_CP = lib.boolToString cfg.enableControlPlane;
          HINDSIGHT_CP_DATAPLANE_API_URL = "http://127.0.0.1:${toString cfg.apiPort}";
          HINDSIGHT_API_LLM_PROVIDER = llm.provider;
          HINDSIGHT_API_LLM_BASE_URL = llm.baseUrl;
          HINDSIGHT_API_LLM_MODEL = llm.model;
        }
        // lib.optionalAttrs cfg.enableApiKeyAuth {
          HINDSIGHT_API_TENANT_EXTENSION = "hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension";
        };
        volumes = [ "${cfg.stateDir}:/home/hindsight/.pg0" ];
        # Container-level health status (observed via `podman healthcheck run
        # hindsight`); the systemd unit itself is considered up once the
        # container starts (sdnotify=conmon).
        extraOptions = [
          "--health-cmd=curl -fsS http://127.0.0.1:${toString cfg.apiPort}/health"
          "--health-interval=30s"
          "--health-timeout=10s"
          "--health-retries=5"
          "--health-start-period=180s"
        ];
      };
    };

    # Tailnet-only: only the tailscale interface may reach the API (and the
    # control plane if enabled). Everything else stays firewalled off.
    networking.firewall.interfaces.tailscale0.allowedTCPPorts = [
      cfg.apiPort
    ]
    ++ lib.optionals cfg.enableControlPlane [ cfg.controlPlanePort ];

    # The embedded pg0 data dir must be writable by the container's uid (1000).
    system.activationScripts.hindsight-state = lib.stringAfter [ "users" ] ''
      install -d -o ${toString cfg.uid} -g ${toString cfg.gid} -m 0755 ${cfg.stateDir}
    '';
  };
}
