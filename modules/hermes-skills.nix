# Vendored, NixOS-corrected skills for the Hermes agent.
#
# Problem: hermes-agent ships ~70 bundled skills in its own repo, many
# irrelevant to this deployment and most written for apt/brew/macOS.
#
# Solution: we do NOT seed the bundled skills at all, and instead expose a
# curated set vendored in ./skills via `skills.external_dirs`:
#   * `.no-bundled-skills` in $HERMES_HOME makes hermes' skill sync a no-op
#     (see tools/skills_sync.py), so the bundled tree never lands in
#     ~/.hermes/skills/.
#   * The vendored skills are built into /nix/store (immutable, rollback-safe)
#     and pointed at with external_dirs. The agent can still author its own
#     skills under ~/.hermes/skills/, which take precedence on name collision.
#
# Update flow: edit a skill in ./skills, commit, deploy. Same loop as the flake.

{
  config,
  pkgs,
  lib,
  ...
}:

let
  cfg = config.services.hermes-agent;

  # Curated skills tree, built into the store. `external_dirs` expects a
  # directory of <category>/<name>/SKILL.md (same layout as upstream skills/).
  vendoredSkills = pkgs.runCommand "hermes-vendored-skills" { } ''
    mkdir -p $out
    cp -r ${./../skills}/* $out/
  '';
in
{
  config = lib.mkIf cfg.enable {

    # Point hermes at the vendored skills (read-only). Local skills under
    # ~/.hermes/skills/ still take precedence on name collision.
    services.hermes-agent.settings.skills.external_dirs = [ "${vendoredSkills}" ];

    # Tools the vendored skills call. opencode = the opencode skill, tmux =
    # the hermes-agent self-management skill. Keep this intentional.
    services.hermes-agent.extraPackages = with pkgs; [
      opencode
      tmux
    ];

    # Stop hermes from seeding its bundled skills into $HERMES_HOME/skills.
    # Must exist before the gateway first syncs; runs after hermes-agent-setup
    # has created $HERMES_HOME.
    system.activationScripts."hermes-skills-setup" = lib.stringAfter [ "hermes-agent-setup" ] ''
      mkdir -p ${cfg.stateDir}/.hermes
      touch ${cfg.stateDir}/.hermes/.no-bundled-skills
      chown ${cfg.user}:${cfg.group} ${cfg.stateDir}/.hermes/.no-bundled-skills
      chmod 0644 ${cfg.stateDir}/.hermes/.no-bundled-skills
    '';
  };
}
