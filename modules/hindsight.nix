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
# Memory quality is tuned for code on the banks listed in `codeBanks` (see
# below): verbatim retain keeps exact code instead of summarising it, and an
# engineering-focused retain mission steers fact extraction. Text-search is
# left on the upstream defaults because it is global — the service is expected
# to serve both code and non-code banks.
#
# Clients reach the API over the tailnet:  http://<tailscale-ip>:8888  with
# `Authorization: Bearer $HINDSIGHT_API_TENANT_API_KEY`. Hermes (same host)
# uses http://127.0.0.1:8888.

{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.hindsight;
  llm = config.hermesDeploy.llm;

  # Retain config applied to each bank in `codeBanks` via
  # PATCH /v1/default/banks/{bank_id}/config.
  codeBankRetainMission =
    "Extract and prioritise engineering facts: exact code snippets, function "
    + "and variable names, API signatures and endpoints, error messages and "
    + "their fixes, build and deployment commands, architectural decisions "
    + "and their rationale, performance measurements, and project "
    + "conventions. Preserve verbatim code. Ignore social pleasantries and "
    + "anything unlikely to matter in six months.";
  codeBankConfigJson = builtins.toJSON {
    updates = {
      retain_extraction_mode = "verbatim";
      retain_mission = codeBankRetainMission;
    };
  };
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

    # ── Code optimisation (per bank) ─────────────────────────────────────
    # Hindsight config is hierarchical: Global (env) -> Tenant -> Bank. The
    # settings that matter for code (extraction mode, retain mission) are
    # bank-configurable, so we leave the global defaults neutral and tune only
    # the banks listed in `codeBanks` — the service is expected to serve both
    # code and non-code banks.
    codeBanks = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = ''
        Banks to tune for code. Applies code-optimised retain config
        (`retain_extraction_mode=verbatim` + an engineering-focused
        `retain_mission`) to exactly these banks via the per-bank config API,
        so banks holding non-code content keep the neutral global defaults.
      '';
    };

    codeBankSetupTimeout = lib.mkOption {
      type = lib.types.int;
      default = 600;
      description = ''
        Seconds the bank-config oneshot waits for the API `/health` before
        giving up. The container health-start-period is 180s, so keep this
        comfortably above it.
      '';
    };

    textSearchExtension = lib.mkOption {
      type = lib.types.enum [
        "native"
        "vchord"
        "pg_textsearch"
        "pgroonga"
        "pg_search"
      ];
      default = "native";
      description = ''
        Text search backend. Global/static — applies to every bank, so it
        stays on the upstream default here. `pg_search` (ParadeDB BM25) is an
        option for operators who want it everywhere; it cannot be set per
        bank.
      '';
    };

    textSearchTokenizer = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        ParadeDB `pg_search` tokenizer for BM25 indexes. Only applies when
        `textSearchExtension = "pg_search"`. `source_code` is tuned for code
        (identifiers, camelCase, snake_case) but is global — it would degrade
        full-text search on non-code banks — so it's opt-in; null uses
        ParadeDB's default (`unicode_words`).
      '';
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
          HINDSIGHT_API_TEXT_SEARCH_EXTENSION = cfg.textSearchExtension;
        }
        // lib.optionalAttrs (cfg.textSearchTokenizer != null) {
          HINDSIGHT_API_TEXT_SEARCH_EXTENSION_PG_SEARCH_TOKENIZER = cfg.textSearchTokenizer;
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

    # Apply code-optimised retain config to exactly the banks in `codeBanks`,
    # once the API is healthy. Global settings stay neutral so non-code banks
    # are unaffected. Idempotent; re-run with `systemctl start
    # hindsight-code-bank-config` after adding banks.
    systemd.services.hindsight-code-bank-config = lib.mkIf (cfg.codeBanks != [ ]) {
      description = "Apply code-optimised retain config to Hindsight code banks";
      wants = [ "podman-hindsight.service" ];
      after = [ "podman-hindsight.service" ];
      wantedBy = [ "multi-user.target" ];
      path = with pkgs; [
        curl
        coreutils
      ];
      serviceConfig = {
        Type = "oneshot";
        EnvironmentFile = lib.optional (cfg.environmentFile != null) cfg.environmentFile;
        # If it can't reach the API (container not healthy in time), leave it
        # failed rather than flapping; `systemctl start` retries it manually.
        Restart = "no";
        TimeoutStartSec = toString (cfg.codeBankSetupTimeout + 120);
      };
      script = "${pkgs.writeShellScript "hindsight-code-bank-config" ''
        set -eu
        api="http://127.0.0.1:${toString cfg.apiPort}"
        key="$HINDSIGHT_API_TENANT_API_KEY"
        banks="${lib.concatStringsSep " " cfg.codeBanks}"
        timeout="${toString cfg.codeBankSetupTimeout}"

        deadline=$(($(date +%s) + timeout))
        until curl -fsS "$api/health" >/dev/null 2>&1; do
          [ "$(date +%s)" -ge "$deadline" ] && {
            echo "hindsight API not healthy after ${toString cfg.codeBankSetupTimeout}s" >&2
            exit 1
          }
          sleep 5
        done

        for bank in $banks; do
          # Ensure the bank exists (no-op if a client already created it).
          curl -fsS -X PUT "$api/v1/default/banks/$bank" \
            -H "Authorization: Bearer $key" >/dev/null 2>&1 || true
          curl -fsS -X PATCH "$api/v1/default/banks/$bank/config" \
            -H "Authorization: Bearer $key" \
            -H "Content-Type: application/json" \
            -d '${codeBankConfigJson}' >/dev/null
          echo "hindsight: configured $bank as a code bank"
        done
      ''}";
    };
  };
}
