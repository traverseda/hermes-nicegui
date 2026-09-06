# xaelwiki — shared markdown notes MCP server.
#
# xaelwiki (https://github.com/traverseda/xaelWiki) is a markdown-native notes
# MCP server: agents read, search, capture, edit, and re-file notes over MCP,
# and a git history makes every change reversible.
#
# Design — vendored source (system lane) + a separate, genuinely low-stakes
# notes vault (content, not code):
#   * The xaelWiki SOURCE (the MCP server code) ships as a git submodule
#     (vendor/xaelWiki), fetched by the `xaelwiki-src` flake input
#     (git+file:./vendor/xaelWiki) and built into the Nix store like any
#     other flake input. It is application code that runs as a network-facing
#     MCP server with write access to the notes vault, so — same as
#     hermes-agent / hermes-nicegui — it is NOT live-editable: a change
#     requires a commit inside the submodule, `nix flake lock --update-input
#     xaelwiki-src`, and a `nixos-rebuild switch`. modules/hermes-deploy.nix's
#     generation-aware rollback resets vendor/xaelWiki's checkout (via `git
#     submodule update`) to match whichever generation is restored.
#   * The NOTES VAULT (the actual markdown content) is what stays fast and
#     low-stakes: a separate git repo cloned from the operator's Codeberg
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
# Only the notes vault is writable state (its own git repo). The app source
# and python env are both immutable NixOS store paths.

{
  config,
  pkgs,
  lib,
  xaelwiki-src,
  ...
}:

let
  cfg = config.services.xaelwiki;

  # Immutable app source: the vendored submodule, fetched and pinned by the
  # xaelwiki-src flake input. A plain Nix store path.
  appSrc = xaelwiki-src;

  # Immutable part: the python environment providing xaelwiki's dependencies.
  pyEnv = pkgs.python314.withPackages (ps: [
    ps.fastmcp
    ps.pyyaml
  ]);

  # codeberg.org ed25519 host key, pinned (avoids TOFU/StrictHostKeyChecking
  # prompts in the service).
  knownHosts = ''
    codeberg.org ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIVIC02vnjFyL+I4RHfvIGNtOgJMe769VTF1VR4EB3ZB
  '';
in
{
  options.services.xaelwiki = {
    enable = lib.mkEnableOption "xaelwiki notes MCP server";

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/xaelwiki";
      description = "Service state: notes vault, SSH deploy key. App source is immutable and does not live here.";
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

    # ── Activation: state dirs only (SSH key + notes vault) — no source ──
    system.activationScripts."xaelwiki-state" = lib.stringAfter [ "users" ] ''
      install -d -o xaelwiki -g xaelwiki -m 0750 ${cfg.stateDir} ${cfg.stateDir}/.ssh ${cfg.stateDir}/notes
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
    # Runs the IMMUTABLE app source (appSrc, from the xaelwiki-src flake
    # input) with the Nix python env. PYTHONPATH points at appSrc's src/
    # package dir so `python -m xaelwiki.server` resolves.
    systemd.services.xaelwiki = {
      description = "xaelwiki notes MCP server";
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
        PYTHONPATH = "${appSrc}/src";
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
        ];
        PrivateTmp = true;
        # The MCP endpoint serves streamable-http and refuses to start without
        # XAEL_AUTH_TOKEN. That token lives in the agenix xaelwiki-env secret —
        # WITHOUT this EnvironmentFile the unit starts with no token and
        # crash-loops ("HTTP transport requires XAEL_AUTH_TOKEN"). This was
        # broken since the module was written (the env file was only wired to
        # the hermes-agent unit, never here).
        EnvironmentFile = lib.optionals (cfg.environmentFile != null) [ cfg.environmentFile ];
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
