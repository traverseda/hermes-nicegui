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
# below): verbatim retain stores byte-exact source chunks (chunk size 800),
# an engineering-focused retain mission + observations mission steer fact
# extraction, and recall boosts BM25 so exact identifier hits beat semantic
# fluff. `enable_auto_consolidation` stays ON so consolidation adds abstracted
# observations on top of the verbatim chunks (additive, not destructive).
# Global settings that help every bank: `simple` text-search dictionary (no
# English stemming — BM25 matches `get_user_id` exactly), ONNX
# multilingual-e5-small embeddings (384d, same as the old bge-small so no
# dimension-change re-embed), and the extraction LLM. Banks not in `codeBanks`
# (e.g. the hermes agent's general/chat bank) keep untouched upstream defaults.
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
  llm = config.hermesDeploy.providers.${config.hermesDeploy.defaultProvider};

  # Retain config applied to each bank in `codeBanks` via
  # PATCH /v1/default/banks/{bank_id}/config.
  #
  # Byte-exact where it matters, deliberately NOT fully byte-exact-only:
  # `enable_auto_consolidation` stays ON (default), so consolidation
  # LLM-resummarises clustered code memories into abstracted observations on
  # top of the verbatim chunks — additive, not destructive. That is the
  # settled trade-off (see hindsight-admin pitfall 9).
  codeBankRetainMission =
    "Extract and prioritise engineering facts: exact code snippets, function "
    + "and variable names, API signatures and endpoints, error messages and "
    + "their fixes, build and deployment commands, architectural decisions "
    + "and their rationale, performance measurements, and project "
    + "conventions. Preserve exact symbols and identifiers verbatim — never "
    + "paraphrase or rename them. Cite error message text exactly as written. "
    + "Also extract the category 'Technical preferences and conventions' "
    + "(e.g. loguru over stdlib logging, type hints required). Ignore social "
    + "pleasantries and anything unlikely to matter in six months.";
  codeBankObservationsMission =
    "Synthesise durable engineering observations from code memories: stable "
    + "architecture patterns, library and tooling choices, and conventions. "
    + "Preserve exact symbol and identifier names; never paraphrase them. "
    + "Keep the observations abstracted summaries layered on top of the "
    + "byte-exact source chunks rather than replacing them.";
  codeBankConfigJson = builtins.toJSON {
    updates = {
      retain_extraction_mode = "verbatim";
      retain_chunk_size = cfg.codeBankRetainChunkSize;
      retain_mission = codeBankRetainMission;
      observations_mission = codeBankObservationsMission;
      enable_auto_consolidation = cfg.codeBankEnableAutoConsolidation;
    };
  };
  # Hand the JSON to curl from a file, NOT via shell single-quote interpolation:
  # the retain_mission prose contains apostrophes (e.g. 'Technical preferences
  # and conventions'), which break out of `-d '...'` and word-split the curl
  # args ("Could not resolve host: preferences"). `curl -d @file` avoids the
  # shell entirely.
  codeBankConfigFile = pkgs.writeText "hindsight-code-bank-config.json" codeBankConfigJson;
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
      default = 30;
      description = ''
        Seconds the bank-config oneshot waits for the API `/health` before
        giving up.
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

    textSearchNativeLanguage = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = "simple";
      description = ''
        PostgreSQL text-search dictionary for the `native` backend
        (`HINDSIGHT_API_TEXT_SEARCH_EXTENSION_NATIVE_LANGUAGE`). Defaults to
        `simple` (no stemming) so BM25 matches identifiers like `get_user_id`
        exactly instead of getting mangled by English stemming. Set to null to
        use the upstream default (`english`).
      '';
    };

    embeddingsProvider = lib.mkOption {
      type = lib.types.str;
      default = "onnx";
      description = ''
        Embeddings backend (`HINDSIGHT_API_EMBEDDINGS_PROVIDER`). Defaults to
        `onnx` (in-process local CPU; no Ollama/TEI/API sidecar) with
        `multilingual-e5-small`, which is 384-dimensional — the same dims as
        the previous bge-small, so no dimension-change wipe/re-embed.
      '';
    };

    embeddingsOnnxModelId = lib.mkOption {
      type = lib.types.str;
      default = "intfloat/multilingual-e5-small";
      description = ''
        ONNX embedding model id (`HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_ID`).
        The default `multilingual-e5-small` pairs with the default E5 query /
        passage prefixes in the image.
      '';
    };

    embeddingsOnnxDimensions = lib.mkOption {
      type = lib.types.int;
      default = 384;
      description = ''
        Embedding dimensions for the ONNX model
        (`HINDSIGHT_API_EMBEDDINGS_ONNX_DIMENSIONS`).
      '';
    };

    recallStrategyBoosts = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = "bm25:high";
      description = ''
        Recall source-priority boosts (`HINDSIGHT_API_RECALL_STRATEGY_BOOSTS`),
        a comma-separated `strategy:level` list. Defaults to `bm25:high` so
        code recall is keyword-driven — exact identifier hits beat semantic
        fluff. Set to null to leave the upstream default (empty = no boosts).
        Note: this is a server-level (static) setting, not per-bank.
      '';
    };

    codeBankRetainChunkSize = lib.mkOption {
      type = lib.types.int;
      default = 800;
      description = ''
        `retain_chunk_size` applied to each bank in `codeBanks`. Source chunks
        are stored byte-exact, so smaller chunks mean finer-grained exact
        retrieval; the docs' own `documents` strategy example uses 800.
      '';
    };

    codeBankEnableAutoConsolidation = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        `enable_auto_consolidation` for each code bank. Kept true (the default)
        deliberately: consolidation LLM-resummarises clustered code memories
        into abstracted observations on top of the byte-exact chunks —
        additive, not destructive. Set to false for true byte-exact-only.
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
          HINDSIGHT_API_LLM_PROVIDER = config.hermesDeploy.hindsight.provider;
          HINDSIGHT_API_LLM_BASE_URL = llm.baseUrl;
          HINDSIGHT_API_LLM_MODEL = llm.model;
          HINDSIGHT_API_TEXT_SEARCH_EXTENSION = cfg.textSearchExtension;
          # Embeddings: in-process ONNX, multilingual-e5-small (384d — same
          # dims as the old bge-small, so no dimension-change re-embed).
          HINDSIGHT_API_EMBEDDINGS_PROVIDER = cfg.embeddingsProvider;
          HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_ID = cfg.embeddingsOnnxModelId;
          HINDSIGHT_API_EMBEDDINGS_ONNX_DIMENSIONS = toString cfg.embeddingsOnnxDimensions;
        }
        // lib.optionalAttrs (cfg.textSearchNativeLanguage != null) {
          # simple dictionary: no English stemming, so BM25 matches
          # `get_user_id` exactly.
          HINDSIGHT_API_TEXT_SEARCH_EXTENSION_NATIVE_LANGUAGE = cfg.textSearchNativeLanguage;
        }
        // lib.optionalAttrs (cfg.recallStrategyBoosts != null) {
          HINDSIGHT_API_RECALL_STRATEGY_BOOSTS = cfg.recallStrategyBoosts;
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

    # Tailnet exposure: the registry opens :apiPort (and :controlPlanePort
    # when the control plane is enabled) on tailscale0 AND fails the build
    # if no credentials are wired. The API key requirement is enforced via
    # `enableApiKeyAuth`; toggling it off is an unauthenticated exposure and
    # must go through the registry's `insecure` opt-in (which also demands
    # the global `allowInsecure` gate).
    hermesDeploy.exposure.services = [
      {
        name = "hindsight-api";
        port = cfg.apiPort;
        auth =
          if cfg.enableApiKeyAuth then
            {
              type = "bearer";
              credentialsConfigured = cfg.environmentFile != null;
            }
          else
            {
              type = "none";
              insecure = false;
            };
      }
    ]
    ++ lib.optionals cfg.enableControlPlane [
      {
        name = "hindsight-control-plane";
        port = cfg.controlPlanePort;
        auth = {
          # HINDSIGHT_CP_ACCESS_KEY from the same env file.
          type = "bearer";
          credentialsConfigured = cfg.environmentFile != null;
        };
      }
    ];

    # The embedded pg0 data dir must be writable by the container's uid (1000).
    system.activationScripts.hindsight-state = lib.stringAfter [ "users" ] ''
      install -d -o ${toString cfg.uid} -g ${toString cfg.gid} -m 0755 ${cfg.stateDir}
    '';

    # Override the hindsight health check default so the port follows
    # `apiPort` — operators who change the port won't need to also fix
    # the health check separately. (If they provide all 5 defaults,
    # they can override hindsight themselves, same as agent/tailnet/etc.)
    services.hermes-deploy.health.checks.hindsight = lib.mkIf cfg.enable {
      what = "hindsight API health (${toString cfg.apiPort}/health)";
      check = ''
        set -uo pipefail
        api="http://127.0.0.1:${toString cfg.apiPort}/health"
        deadline=$((SECONDS + 90))
        while ! http_code=$(curl -sf --max-time 5 -o /dev/null -w "%{http_code}" "$api" 2>/dev/null || echo "000"); do
          if [ "$http_code" = "401" ] || [ "$http_code" = "200" ]; then break; fi
          if (( SECONDS >= deadline )); then exit 1; fi
          sleep 5
        done
      '';
    };

    # Apply code-optimised retain config to exactly the banks in `codeBanks`,
    # once the API is healthy. Global settings stay neutral so non-code banks
    # are unaffected. NOT a wantedBy unit — should only be run manually
    # (systemctl start hindsight-code-bank-config) for initial bank setup or
    # after banks are modified. Must NOT block NixOS activation.
    systemd.services.hindsight-code-bank-config = lib.mkIf (cfg.codeBanks != [ ]) {
      description = "Apply code-optimised retain config to Hindsight code banks";
      wants = [ "podman-hindsight.service" ];
      after = [ "podman-hindsight.service" ];
      wantedBy = [ ];
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
        : "''${HINDSIGHT_API_TENANT_API_KEY:?hindsight env missing HINDSIGHT_API_TENANT_API_KEY}"
        api="http://127.0.0.1:${toString cfg.apiPort}"
        key="$HINDSIGHT_API_TENANT_API_KEY"
        banks="${lib.concatStringsSep " " cfg.codeBanks}"
        timeout="${toString cfg.codeBankSetupTimeout}"

        # Health check: distinguish "connection refused" (container starting,
        # keep retrying) from "HTTP error" (API running but broken, fail now).
        deadline=$(($(date +%s) + timeout))
        while true; do
          http_code=$(curl -sk -o /dev/null -w "%{http_code}" "$api/health" 2>/dev/null || echo "000")
          if [ "$http_code" = "200" ]; then
            break
          elif [ "$http_code" != "000" ]; then
            echo "hindsight API returned HTTP $http_code on /health — failing immediately (not retrying)" >&2
            exit 1
          fi
          [ "$(date +%s)" -ge "$deadline" ] && {
            echo "hindsight API not healthy after $timeout s" >&2
            exit 1
          }
          sleep 3
        done

        for bank in $banks; do
          # Ensure the bank exists (no-op if a client already created it).
          http_code=$(curl -sk -o /dev/null -w "%{http_code}" \
            -X PUT "$api/v1/default/banks/$bank" \
            -H "Authorization: Bearer $key" 2>/dev/null || echo "000")
          if [ "$http_code" != "200" ] && [ "$http_code" != "409" ]; then
            echo "hindsight failed to create bank '$bank': HTTP $http_code" >&2
          fi
          http_code=$(curl -s -o /dev/stderr -w "%{http_code}" \
            -X PATCH "$api/v1/default/banks/$bank/config" \
            -H "Authorization: Bearer $key" \
            -H "Content-Type: application/json" \
            -d @${codeBankConfigFile} 2>/dev/null || echo "000")
          if [ "$http_code" != "200" ]; then
            echo "hindsight failed to config bank '$bank': HTTP $http_code" >&2
            exit 1
          fi
          echo "hindsight: configured $bank as a code bank"
        done
      ''}";
    };
  };
}
