# Git-backed content store + `hermes-tool` fast-path for the Hermes agent.
#
# THIS IS THE "CONTENT" LANE — separate from the Nix "system" lane.
#
# The problem it solves: the core unit of work here is *creating new tools*
# (skills, scripts, MCP servers) that are mostly NOT critical for the agent to
# run. But the deploy flow forces every change through a Nix rebuild + new
# generation + full gateway restart. That is monolithic and slow.
#
# This module gives the agent a fast, safe lane:
#   * A git repo at ${stateDir}/content holds agent-authored skills/ and bin/.
#     Skills are symlinked into $HERMES_HOME/skills (hermes picks them up next
#     session — NO restart). Bin is appended to the gateway PATH (available
#     next turn — NO restart). MCP registrations edit config.yaml (survives
#     rebuilds because the merge preserves user keys) and need ONLY a gateway
#     restart — not a nixos-rebuild.
#   * Every mutation goes through `hermes-tool`, which AUTO-COMMITS. Git is
#     the rollback ledger for content, exactly as Nix generations are the
#     ledger for the system.
#   * Recovery is a store binary. `hermes-tool revert` (from /nix/store,
#     immutable) restores the last-known-good git tag `content-good` AND the
#     known-good config.yaml, then restarts the gateway. It depends on nothing
#     the agent can touch, so even a fully broken content store is fixable —
#     the agent cannot brick itself in a way it cannot fix.
#   * The deploy watchdog runs `hermes-tool revert` as a cheap FIRST recovery
#     attempt BEFORE falling back to a Nix generation rollback (see
#     services.hermes-deploy.contentRecovery). Content failures are reverted
#     at content granularity; only system failures cost a generation.
#
# Relationship to the system lane:
#   * Nix still owns: the gateway unit, packages (extraPackages), the firewall,
#     secrets, and the immutable vendored-skill baseline (external_dirs).
#   * The content lane can NEVER change those — skills/tools/mcp additions are
#     just files the running agent reads. So a bad content change degrades
#     tooling at worst; it cannot take down the system, and `hermes-tool
#     revert` undoes it.
#
# Safety invariants (why it can't brick itself):
#   1. hermes-tool is in /nix/store — immutable, always present, always works.
#   2. Every mutation auto-commits — git is the content rollback ledger.
#   3. content-good tag + config.yaml snapshot = a mechanical "last known good".
#   4. The watchdog tries content-revert before generation-rollback, and
#      generation-rollback (hermes-rollback) remains the ultimate floor.
#   5. Local skills shadow external_dirs on collision, but the Nix vendored
#      baseline is still scanned alongside — deleting a local skill only
#      un-shadows the immutable original.

{
  config,
  pkgs,
  lib,
  ...
}:

let
  agentCfg = config.services.hermes-agent;
  cfg = config.services.hermes-tools;

  contentDir = cfg.contentDir;
  hermesHome = "${agentCfg.stateDir}/.hermes";
  goodTag = "content-good";

  # hermes-tool: a store binary built from scripts/hermes-tool.sh with the
  # concrete paths substituted in. This is the recovery floor — it must exist
  # and work even if the agent has deleted every skill and tool.
  hermesTool = pkgs.replaceVars ./../scripts/hermes-tool.sh {
    inherit contentDir hermesHome goodTag;
    gatewayService = "hermes-agent";
  };
  hermesToolPackage = pkgs.runCommand "hermes-tool" { } ''
    mkdir -p $out/bin
    install -m 0755 ${hermesTool} $out/bin/hermes-tool
  '';

  # Activation scripts run with a PATH that has NO git (only coreutils,
  # gnused, ...), so any git call there must use an absolute store path. The
  # hermes-content-commit systemd service sets its own `path = [ git ]`, so
  # it can keep calling bare `git` — but the activation snippet below cannot.
  gitBin = "${lib.getExe pkgs.git}";

  # Identity used for content-store auto-commits (repo-local git config).
  gitIdentity = ''
    ${gitBin} -C ${contentDir} config user.name "hermes-content"
    ${gitBin} -C ${contentDir} config user.email "hermes-content@localhost"
  '';
in
{
  options.services.hermes-tools = {
    enable = lib.mkEnableOption "git-backed content store + hermes-tool fast-path tooling";

    contentDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/hermes/content";
      description = ''
        Git-backed directory holding the agent's authored skills/ and bin/.
        This is the "content lane" — independent of Nix generations, with its
        own rollback (git) and its own recovery command (hermes-tool revert).
      '';
    };

    autoCommitInterval = lib.mkOption {
      type = lib.types.str;
      default = "15min";
      description = "How often the content-store auto-commit timer runs.";
    };
  };

  config = lib.mkIf (cfg.enable && agentCfg.enable) {
    # ── Content store: init git repo, symlink skills, self-heal ─────────
    # Runs on every activation so a deleted/wiped store self-heals back to an
    # empty-but-working repo with a fresh content-good tag.
    system.activationScripts."hermes-content-store" =
      lib.stringAfter
        [
          "hermes-agent-setup"
          "hermes-skills-setup"
        ]
        ''
          mkdir -p ${contentDir}/skills ${contentDir}/bin
          chown -R ${agentCfg.user}:${agentCfg.group} ${contentDir}
          if [ ! -d ${contentDir}/.git ]; then
            echo "hermes-tools: initialising content store at ${contentDir}"
            ${gitBin} -C ${contentDir} init -q
            ${gitIdentity}
            ${gitBin} -C ${contentDir} add -A
            ${gitBin} -C ${contentDir} commit -q -m "init content store" || true
            ${gitBin} -C ${contentDir} tag ${goodTag} || true
          fi
          # Link $HERMES_HOME/skills -> content/skills so authored skills are
          # git-backed AND picked up by hermes next session (no restart).
          if [ -e ${hermesHome}/skills ] && [ ! -L ${hermesHome}/skills ]; then
            echo "hermes-tools: folding existing ${hermesHome}/skills into the content store"
            cp -a ${hermesHome}/skills/. ${contentDir}/skills/ 2>/dev/null || true
            rm -rf ${hermesHome}/skills
          fi
          ln -sfn ${contentDir}/skills ${hermesHome}/skills
          chown -h ${agentCfg.user}:${agentCfg.group} ${hermesHome}/skills
        '';

    # ── hermes-tool on PATH for the agent (gateway service) and operator ──
    services.hermes-agent.extraPackages = [ hermesToolPackage ];
    environment.systemPackages = [ hermesToolPackage ];

    # ── Content bin is APPENDED to the gateway PATH (lowest precedence, so
    #    an agent-authored `git` can never shadow the system one). ──────
    systemd.services.hermes-agent.path = lib.mkAfter [ "${contentDir}/bin" ];

    # ── Watchdog/deploy recovery hook: try content revert BEFORE a Nix
    #    generation rollback. This option is read by modules/hermes-deploy.nix.
    services.hermes-deploy.contentRecovery = lib.mkDefault "${hermesToolPackage}/bin/hermes-tool revert";

    # ── Periodic auto-commit (15min) so direct skill/bin edits are also
    #    captured in git, not just hermes-tool-mediated ones. ───────────
    systemd.services.hermes-content-commit = {
      description = "Auto-commit changes to the hermes content store";
      path = with pkgs; [
        git
        coreutils
      ];
      serviceConfig = {
        Type = "oneshot";
        User = agentCfg.user;
        Group = agentCfg.group;
      };
      script = ''
        if [ -d ${contentDir}/.git ]; then
          git -C ${contentDir} add -A
          if ! git -C ${contentDir} diff --cached --quiet; then
            ${gitIdentity}
            git -C ${contentDir} commit -q -m "auto-commit $(date -Is)"
          fi
        fi
      '';
    };

    systemd.timers.hermes-content-commit = {
      description = "Periodic auto-commit of the hermes content store";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "5min";
        OnUnitActiveSec = cfg.autoCommitInterval;
        Persistent = true;
      };
    };
  };
}
