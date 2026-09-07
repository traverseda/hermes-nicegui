# Per-secret agenix store + per-consumer env-file aggregation.
#
# The replacement for the old unified config.age blob: secrets now live ONE
# PER FILE as secrets/<NAME>.age (encrypted to the operator + LXC host keys),
# declared here in plaintext in secrets/manifest. This module:
#
#   1. Reads secrets/manifest (TAB-separated: name, consumers, required,
#      placeholder, purpose) and generates one `age.secrets."<NAME>"` entry
#      per secret -> /run/agenix/<NAME> (decrypted at activation, 0400 root).
#   2. After agenix has decrypted, aggregates those per-secret files into
#      one env file per CONSUMER: /run/agenix/<consumer>.env containing only
#      the KEY=value lines that consumer uses. Consumers:
#        hermes        -> default profile .env (gateway + dashboard + ha profile + opencode children)
#        hindsight     -> Hindsight container env
#        nicegui       -> hermes-nicegui unit env (kanban creds + gateway token)
#        xaelwiki      -> xaelwiki MCP server env (bearer token)
#      Duplicated KEY=value lines across aggregates are idempotent (setenv-
#      style semantics), so a secret consumed by two services simply appears
#      in both files.
#   3. Ships `hermes-secrets` (edit/check/doctor) and wires the deploy-time
#      gate into hermes-deploy (run only when this module is enabled).
#
# Why per-file: editing one secret no longer rewrites/endangers the others
# (the blob failure mode the old design had), and the manifest + tooling
# turn "which secret changed / is missing" into a plaintext, reviewable
# question again.
#
# Safety: the activation aggregate only ever READS /run/agenix/* files that
# agenix itself just wrote. It writes under /run/agenix (root, 0400). The
# vendored hermes-agent module's `hermes-agent-setup` builds
# $HERMES_HOME/.env from environmentFiles, so we order it after
# hermes-secrets-env (via deps) to guarantee it sees the finished hermes.env.

{
  config,
  pkgs,
  lib,
  ...
}:

let
  cfg = config.services.hermes-secrets;

  # ── Parse secrets/manifest ────────────────────────────────────────────
  # name<TAB>consumers<TAB>required<TAB>placeholder<TAB>purpose
  manifestText = builtins.readFile ./../secrets/manifest;
  manifestLines = lib.filter (l: l != "" && !lib.hasPrefix "#" l) (lib.splitString "\n" manifestText);
  parseLine =
    l:
    let
      p = lib.splitString "\t" l;
    in
    {
      name = lib.elemAt p 0;
      consumers = lib.splitString "," (lib.elemAt p 1);
      required = lib.elemAt p 2 == "1";
      placeholder = lib.elemAt p 3;
      purpose = lib.elemAt p 4;
    };
  manifest = map parseLine manifestLines;

  # ── Consumer -> aggregate env file ────────────────────────────────────
  consumerFile = {
    hermes = "/run/agenix/hermes.env";
    hindsight = "/run/agenix/hindsight.env";
    nicegui = "/run/agenix/hermes-nicegui.env";
    xaelwiki = "/run/agenix/xaelwiki.env";
  };

  consumers = builtins.attrNames consumerFile;

  # secrets consumed by `c`
  forConsumer = c: lib.filter (s: lib.elem c s.consumers) manifest;

  # Build the aggregation shell for one consumer: `NAME=value` line per
  # secret, value read from the agenix-decrypted /run/agenix/<NAME>.
  # Each file is a BUILD: content assembled from the manifest's key list,
  # then one atomic `install`. No cat-chains, no source env files echoing
  # into each other. Values themselves never exist in the Nix store — they
  # are read from the /run/agenix/<NAME> files agenix just wrote.
  aggregateConsumer =
    c: file:
    let
      body = lib.concatMapStringsSep "\n" (
        s: "printf '%s=' \"${s.name}\"; cat /run/agenix/${s.name}; printf '\\n'"
      ) (forConsumer c);
    in
    ''
      : > ${file}.tmp
      chmod 0400 ${file}.tmp
      umask 077
      ${body} >> ${file}.tmp
      mv -f ${file}.tmp ${file}
    '';

  aggregateAll = lib.concatMapStringsSep "\n" (c: aggregateConsumer c consumerFile.${c}) consumers;

  # ── hermes-secrets tool (store-built, mirrors hermes-tool) ────────────
  # Two layers so the store binary is self-contained:
  #   * hermesSecretsRaw  — the pristine script (real logic).
  #   * hermesSecretsPackage — a wrapper on PATH that prepends age's bin
  #     (age may not be on the caller's PATH on the build machine) then
  #     execs the raw script.
  hermesSecretsRaw = pkgs.runCommand "hermes-secrets" { } ''
    mkdir -p $out/bin
    cp ${./../scripts/hermes-secrets} $out/bin/hermes-secrets
    chmod +x $out/bin/hermes-secrets
  '';
  hermesSecretsWrapper = pkgs.writeShellScript "hermes-secrets-wrapper" ''
    export PATH=${pkgs.age}/bin:''$PATH
    exec ${hermesSecretsRaw}/bin/hermes-secrets ''$@
  '';
  hermesSecretsPackage = pkgs.runCommand "hermes-secrets" { } ''
    mkdir -p $out/bin
    cp ${hermesSecretsWrapper} $out/bin/hermes-secrets
    chmod +x $out/bin/hermes-secrets
  '';

  gateCmd = "${hermesSecretsPackage}/bin/hermes-secrets check --repo ${config.services.hermes-deploy.repoDir}";
in
{
  options.services.hermes-secrets = {
    enable = lib.mkEnableOption ''
      per-secret agenix store + per-consumer env aggregation
      (secrets/manifest as the plaintext registry).
    '';

    # Deploy-time gate override (normally derived from the module). Set to
    # "" to skip the secret check during deploys (not recommended).
    deployGate = lib.mkOption {
      type = lib.types.str;
      default = gateCmd;
      description = "Command run by hermes-deploy to validate secrets before switching.";
    };
  };

  config = lib.mkIf cfg.enable {
    # ── One age.secrets entry per manifest secret -> /run/agenix/<NAME> ──
    # This is the DIRECT inclusion mechanism: every consumer below points at
    # an agenix-decrypted path under /run/agenix — no secret ever touches
    # /nix/store. Env-style consumers read the per-consumer aggregate built
    # below; file-style consumers (tailscale authKeyFile, cloudflared
    # tokenFile, xaelwiki sshKeyFile) reference /run/agenix/<NAME> directly.
    age.secrets = builtins.listToAttrs (
      map (s: {
        name = s.name;
        value = {
          file = ../secrets/${s.name}.age;
          owner = "root";
          mode = "0400";
        };
      }) manifest
    );

    # ── Build per-consumer env files from the age-decrypted values ───────
    # One atomic `install` per consumer — the file is BUILT from the
    # manifest's key list (deterministic, sorted in manifest order), not
    # assembled by appending other env files into each other.
    # Runs after agenix's decryption chain (agenixNewGeneration/agenixInstall/
    # agenixChown -> agenix) so /run/agenix/<NAME> all exist.
    system.activationScripts."hermes-secrets-env" = {
      deps = [ "agenix" ];
      text = ''
        set -eu
        ${aggregateAll}
      '';
    };

    # hermes-agent-setup (vendored module) builds $HERMES_HOME/.env from
    # environmentFiles — must see the finished hermes.env.
    system.activationScripts."hermes-agent-setup".deps = lib.mkAfter [ "hermes-secrets-env" ];

    # ── The agent's .env is OUR build, and only ours ────────────────────
    # While hermes-secrets is enabled, hermes-agent reads exactly one
    # environment source: the hermes aggregate below (a full, manifest-built
    # copy of every hermes-consumed secret, plus whatever Nix `environment`
    # attrs the vendored module adds). lib.mkForce overrides any other module
    # that tried to append its own env file into the chain (dashboard,
    # hermes-ha, xaelwiki all did) — their secrets are already members of the
    # hermes aggregate, so those appends were pure duplication.
    services.hermes-agent.environmentFiles = lib.mkForce [ "/run/agenix/hermes.env" ];

    # hermes-secrets on PATH for the operator + agent (edit/check/doctor).
    environment.systemPackages = [ hermesSecretsPackage ];
    services.hermes-agent.extraPackages = [ hermesSecretsPackage ];

    # Deploy-time secret gate (only when this module is enabled).
    services.hermes-deploy.deployPreChecks = [ cfg.deployGate ];
  };
}
