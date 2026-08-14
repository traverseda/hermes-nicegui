# xaelwiki — shared, editable markdown notes MCP server.
#
# xaelwiki (https://github.com/traverseda/xaelWiki) is a markdown-native notes
# MCP server: agents read, search, capture, edit, and re-file notes over MCP,
# and a git history makes every change reversible.
#
# Design — FAST, LOW-STAKES, INDEPENDENTLY REVERTIBLE:
#   * The xaelWiki SOURCE ships as a git submodule (vendor/xaelWiki), pinned in
#     this repo. On the LXC it is materialized into the deploy checkout and
#     SYMLINKED into the run location (${stateDir}/src). The service runs the
#     checkout directly via a Nix python env — so editing the submodule files
#     takes effect on `systemctl restart xaelwiki`, with NO nixos-rebuild.
#   * That is deliberate: notes are non-critical. A broken xaelwiki does not
#     brick the agent, and the source is its own git repo, so rollback is
#     `git checkout <good-rev>` (or git revert) inside the submodule — entirely
#     independent of Nix generations / system rollback. The deploy flow never
#     force-updates the submodule, so local edits survive deploys.
#   * The NOTES VAULT is a separate git repo cloned from the operator's Codeberg
#     remote (notes.git) with a dedicated SSH deploy key. The server auto-pulls
#     before each mutation and auto-pushes after, keeping the LXC vault in sync
#     with ~/Code/personal/xaelWiki/notes.
#   * EDITEABLE BY DEFAULT. We deliberately do NOT set XAEL_READ_ONLY — the
#     hermes agent gets the full write surface (capture / append / update /
#     move / tag / undo). Safety is inherited from xaelwiki: no delete
#     (archive instead), auto-commit on every mutation, optimistic concurrency
#     via `revision`, undo reverting through git.
#   * Credential enforcement. The MCP endpoint is registered in
#     hermesDeploy.exposure.services (bearer auth) so the build fails if the
#     xaelwiki secret isn't wired. hermes-agent connects over 127.0.0.1 with the
#     same bearer token from the agenix env file.
#   * Secrets come from agenix: the bearer token (xaelwiki-env) and the SSH
#     deploy key for the notes repo (xaelwiki-ssh). Never in Nix config.
#
# Only the notes vault and the submodule working tree are writable state; both
# are git repos. Everything else (python env, units) is immutable NixOS.

{
  config,
  pkgs,
  lib,
  ...
}:

let
  cfg = config.services.xaelwiki;

  # Immutable part: the python environment providing xaelwiki's dependencies.
  # The code itself comes from the editable submodule checkout.
  pyEnv = pkgs.python314.withPackages (ps: [
    ps.fastmcp
    ps.pyyaml
  ]);

  # codeberg.org ed25519 host key, pinned (avoids TOFU/StrictHostKeyChecking
  # prompts in the service).
  knownHosts = ''
    codeberg.org ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIVIC02vnjFyL+I4RHfvIGNtOgJMe769VTF1VR4EB3ZB
  '';

  # Path of the submodule checkout inside the deploy repo on the LXC.
  submodulePath = "${config.services.hermes-deploy.repoDir}/vendor/xaelWiki";
in
{
  options.services.xaelwiki = {
    enable = lib.mkEnableOption "xaelwiki notes MCP server";

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/xaelwiki";
      description = "Service state: run-location symlink, notes vault, SSH deploy key.";
    };

    port = lib.mkOption {
      type = lib.types.int;
      default = 8000;
      description = "Port the MCP HTTP endpoint listens on (tailnet-only).";
    };

    notesUrl = lib.mkOption {
      type = lib.types.str;
      default = "ssh://git@codeberg.org/traverseda/notes.git";
      description = "SSH URL of the shared notes vault git repo.";
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        Agenix-decrypted env file with the MCP bearer token:
          XAEL_AUTH_TOKEN=<token>
        Used both by the server (HTTP auth) and by hermes-agent's MCP client
        (via ''${XAEL_AUTH_TOKEN} header interpolation, resolved from .env at
        runtime — the literal token never reaches config.yaml or the store).
      '';
    };

    sshKeyFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        Agenix-decrypted file containing the SSH private key (ed25519) for the
        notes repo deploy key. Must be able to clone/push the notesUrl remote.
      '';
    };

    readOnly = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Serve in read-only mode (removes every write tool). Defaults to false
        so the agent can edit notes — this is deliberate; undo is git-backed.
      '';
    };

    autoPush = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Push the notes vault after every mutation (keep in sync with the remote).";
    };
  };

  config = lib.mkIf cfg.enable {
    # ── Service user + state ─────────────────────────────────────────────
    users.users.xaelwiki = {
      isSystemUser = true;
      group = "xaelwiki";
      home = cfg.stateDir;
      createHome = true;
    };
    users.groups.xaelwiki = { };

    # ── Tailnet exposure via the registry (credential-enforced) ─────────
    hermesDeploy.exposure.services = [
      {
        name = "xaelwiki";
        port = cfg.port;
        auth = {
          type = "bearer";
          credentialsConfigured = cfg.environmentFile != null;
        };
      }
    ];

    # ── Wire into hermes-agent as an MCP server ─────────────────────────
    # The token is interpolated from $HERMES_HOME/.env at runtime
    # (hermes' ${VAR} header interpolation), so the literal token never
    # lands in config.yaml or the Nix store — only the placeholder does.
    services.hermes-agent.environmentFiles = lib.optionals (cfg.environmentFile != null) [
      cfg.environmentFile
    ];
    services.hermes-agent.mcpServers.xaelwiki = {
      url = "http://127.0.0.1:${toString cfg.port}/mcp";
      headers.Authorization = "Bearer \${XAEL_AUTH_TOKEN}";
    };

    # ── Activation: materialize submodule + symlink into run location ───
    # Runs as root. `git submodule update --init` is only invoked when the
    # checkout is missing (first boot / fresh clone), never force-updated, so
    # local edits to the submodule survive deploys — that is the point.
    system.activationScripts."xaelwiki-source" = lib.stringAfter [ "users" ] ''
      deploy_repo=${lib.escapeShellArg config.services.hermes-deploy.repoDir}
      if [ -d "$deploy_repo/.git" ] && [ ! -e ${lib.escapeShellArg "${submodulePath}/.git"} ]; then
        echo "xaelwiki: initializing source submodule"
        git -C "$deploy_repo" submodule update --init vendor/xaelWiki || true
      fi
      if [ -d ${lib.escapeShellArg submodulePath} ]; then
        chown -R xaelwiki:xaelwiki ${lib.escapeShellArg submodulePath}
      fi
      mkdir -p ${cfg.stateDir}
      ln -sfn ${lib.escapeShellArg submodulePath} ${cfg.stateDir}/src
      chown -h xaelwiki:xaelwiki ${cfg.stateDir}/src
      install -d -o xaelwiki -g xaelwiki -m 0750 ${cfg.stateDir}/.ssh ${cfg.stateDir}/notes
    '';

    # ── Vault bootstrap: SSH key + clone the notes repo ─────────────────
    systemd.services.xaelwiki-vault = {
      description = "xaelwiki notes vault bootstrap (clone + SSH key)";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];
      path = with pkgs; [
        git
        openssh
        coreutils
      ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        User = "xaelwiki";
      };
      script = ''
        set -eu
        ${lib.optionalString (cfg.sshKeyFile != null) ''
          install -o xaelwiki -g xaelwiki -m 0600 ${cfg.sshKeyFile} ${cfg.stateDir}/.ssh/id_ed25519
        ''}
        install -o xaelwiki -g xaelwiki -m 0644 /dev/stdin ${cfg.stateDir}/.ssh/known_hosts <<'XAEL_KNOWN_HOSTS'
        ${knownHosts}
        XAEL_KNOWN_HOSTS
        if [ ! -d ${cfg.stateDir}/notes/.git ]; then
          echo "xaelwiki: cloning notes vault from ${cfg.notesUrl}"
          git -C ${cfg.stateDir}/notes init -b main
          git -C ${cfg.stateDir}/notes remote add origin ${cfg.notesUrl}
          GIT_SSH_COMMAND="ssh -i ${cfg.stateDir}/.ssh/id_ed25519 -o StrictHostKeyChecking=yes" \
            git -C ${cfg.stateDir}/notes pull origin main || true
          chown -R xaelwiki:xaelwiki ${cfg.stateDir}/notes
        fi
      '';
    };

    # ── The MCP server itself ────────────────────────────────────────────
    # Runs the EDITABLE submodule checkout (via the run-location symlink)
    # with the immutable Nix python env. PYTHONPATH points at the checkout's
    # src/ package dir so `python -m xaelwiki.server` resolves.
    systemd.services.xaelwiki = {
      description = "xaelwiki notes MCP server (editable)";
      after = [
        "xaelwiki-vault.service"
        "network-online.target"
      ];
      wants = [
        "xaelwiki-vault.service"
        "network-online.target"
      ];
      wantedBy = [ "multi-user.target" ];

      environment = {
        HOME = cfg.stateDir;
        PYTHONPATH = "${cfg.stateDir}/src/src";
        XAEL_TRANSPORT = "streamable-http";
        XAEL_HOST = "0.0.0.0";
        XAEL_PORT = toString cfg.port;
        XAEL_PATH = "/mcp";
        XAEL_ROOT = cfg.stateDir;
        XAEL_READ_ONLY = if cfg.readOnly then "1" else "0";
        XAEL_AUTO_PUSH = if cfg.autoPush then "1" else "0";
        GIT_SSH_COMMAND = "ssh -i ${cfg.stateDir}/.ssh/id_ed25519 -o StrictHostKeyChecking=yes";
      };

      serviceConfig = {
        User = "xaelwiki";
        Group = "xaelwiki";
        WorkingDirectory = cfg.stateDir;
        ExecStart = "${pyEnv}/bin/python -m xaelwiki.server";
        Restart = "always";
        RestartSec = 5;
        UMask = "0007";
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = false;
        ReadWritePaths = [
          cfg.stateDir
          config.services.hermes-deploy.repoDir
        ];
        PrivateTmp = true;
      };

      path = with pkgs; [
        git
        openssh
      ];
    };

    warnings = lib.optionals (cfg.environmentFile == null) [
      "services.xaelwiki: no environmentFile set — the MCP endpoint cannot start over HTTP without XAEL_AUTH_TOKEN (and the exposure registry will refuse the build)."
    ];
  };
}
