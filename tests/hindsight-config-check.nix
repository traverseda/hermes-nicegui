# Fast, build-light verification of the Hindsight service wiring.
#
# Unlike a booted NixOS test, this evals the hindsight + llm modules and
# asserts on the generated systemd unit / podman script / oneshot script
# *text*, so it builds only those small derivations — seconds, not a full VM.
#
# Run with:  nix build .#checks.x86_64-linux.hindsight-config-check

{ nixpkgs }:
let
  system = "x86_64-linux";
  inherit (nixpkgs) lib;
  pkgs = nixpkgs.legacyPackages.${system};

  cfg =
    (lib.nixosSystem {
      inherit system;
      modules = [
        ../modules/llm.nix
        ../modules/hindsight.nix
        {
          services.hindsight = {
            enable = true;
            autoStart = false; # don't pull the ~2GB image
            environmentFile = "/dummy/env";
            codeBanks = [ "code" ];
          };
        }
      ];
    }).config;

  # Generated artifacts to assert on (small strings / store paths).
  podmanScriptFile = pkgs.writeText "podman-hindsight.script" cfg.systemd.services.podman-hindsight.script;
  podmanUnitFile =
    pkgs.writeText "podman-hindsight.unit"
      cfg.systemd.units."podman-hindsight.service".text;
  oneshotScriptPath = cfg.systemd.services.hindsight-code-bank-config.script; # store path
  oneshotUnitFile =
    pkgs.writeText "hindsight-code-bank-config.unit"
      cfg.systemd.units."hindsight-code-bank-config.service".text;
  podmanWantedBy =
    builtins.concatStringsSep ","
      cfg.systemd.units."podman-hindsight.service".wantedBy;

  # grep -q with a clear message on failure.
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
pkgs.runCommand "hindsight-config-check"
  {
    inherit
      podmanScriptFile
      podmanUnitFile
      oneshotScriptPath
      oneshotUnitFile
      ;
    inherit podmanWantedBy;
    passAsFile = [ "podmanWantedBy" ];
  }
  ''
    cat $podmanScriptFile > podman.script
    cat $podmanUnitFile > podman.unit
    cat $oneshotScriptPath > oneshot.script
    cat $oneshotUnitFile > oneshot.unit

    # ── podman container wiring ────────────────────────────────────────
    ${need "ghcr.io/vectorize-io/hindsight@sha256:" "podman.script"}
    ${need "--network=host" "podman.script"}
    ${need "-v /var/lib/hindsight:/home/hindsight/.pg0" "podman.script"}
    ${need "--env-file /dummy/env" "podman.script"}
    ${need "--health-cmd=curl -fsS http://127.0.0.1:8888/health" "podman.script"}
    ${need "-e HINDSIGHT_API_LLM_MODEL=deepseek-v4-flash" "podman.script"}
    ${need "-e HINDSIGHT_API_LLM_BASE_URL=https://opencode.ai/zen/go/v1" "podman.script"}
    ${need "HINDSIGHT_API_TENANT_EXTENSION" "podman.script"}

    # Shared global changes (help code and non-code banks alike).
    ${need "-e HINDSIGHT_API_TEXT_SEARCH_EXTENSION=native" "podman.script"}
    ${need "-e HINDSIGHT_API_TEXT_SEARCH_EXTENSION_NATIVE_LANGUAGE=simple" "podman.script"}
    ${need "-e HINDSIGHT_API_EMBEDDINGS_PROVIDER=onnx" "podman.script"}
    ${need "-e HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_ID=intfloat/multilingual-e5-small" "podman.script"}
    ${need "-e HINDSIGHT_API_EMBEDDINGS_ONNX_DIMENSIONS=384" "podman.script"}
    ${need "-e HINDSIGHT_API_RECALL_STRATEGY_BOOSTS=bm25:high" "podman.script"}

    # Global settings stay neutral (the service also hosts non-code banks).
    ${mustNot "HINDSIGHT_API_RETAIN_EXTRACTION_MODE" "podman.script"}
    ${mustNot "HINDSIGHT_API_RETAIN_MISSION" "podman.script"}

    # autoStart=false -> unit must not be pulled into multi-user.target.
    if [ -n "$podmanWantedBy" ]; then
      echo "UNEXPECTED: podman-hindsight wantedBy=$podmanWantedBy" >&2
      exit 1
    fi

    # ── per-bank code tuning oneshot ───────────────────────────────────
    ${need "hindsight-code-bank-config" "oneshot.unit"}
    ${need "After=podman-hindsight.service" "oneshot.unit"}
    ${need "Wants=podman-hindsight.service" "oneshot.unit"}
    ${need "WantedBy=multi-user.target" "oneshot.unit"}
    ${need "EnvironmentFile=/dummy/env" "oneshot.unit"}
    ${need "Type=oneshot" "oneshot.unit"}

    # The oneshot script (executed on a real box) carries the code config.
    ${need "banks=\"code\"" "oneshot.script"}
    ${need "\"retain_extraction_mode\":\"verbatim\"" "oneshot.script"}
    ${need "\"retain_chunk_size\":800" "oneshot.script"}
    ${need "\"retain_mission\"" "oneshot.script"}
    ${need "\"observations_mission\"" "oneshot.script"}
    ${need "\"enable_auto_consolidation\":true" "oneshot.script"}
    ${need "/health" "oneshot.script"}

    touch "$out"
    echo "hindsight-config-check passed"
  ''
