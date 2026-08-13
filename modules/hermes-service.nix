# Shared Hermes agent + Tailscale service configuration.
#
# Used both by the Proxmox LXC host (hosts/hermes/configuration.nix) and by
# the local test VM (tests/vm-configuration.nix) so behaviour is identical.
#
# Design: everything here is declarative. `settings` is deep-merged into
# $HERMES_HOME/config.yaml on every activation; secrets come from agenix and
# are merged into $HERMES_HOME/.env. The gateway is a hardened systemd unit.
# Native (non-container) mode is used: the agent cannot apt/pip/npm install
# itself, which is what makes "the bot changes itself" rollback-safe — new
# capabilities have to be declared here, go through git, and become a new
# generation.

{
  config,
  pkgs,
  lib,
  ...
}:

let
  cfg = config.services.hermes-agent;
in
{
  options.hermesDeploy = {
    # Overridable model identifier. Kept as an option so the test VM can
    # inject a fake provider instead of a real one.
    model = lib.mkOption {
      type = lib.types.str;
      default = "openrouter/anthropic/claude-sonnet-4";
      description = "Default model identifier for Hermes.";
    };
  };

  config = {
    services.hermes-agent = {
      enable = true;

      # Put the `hermes` CLI on PATH and export HERMES_HOME system-wide so
      # the interactive CLI shares state (sessions, skills, cron) with the
      # gateway service.
      addToSystemPackages = true;

      # Declarative config, deep-merged into config.yaml. Values here win
      # over anything the agent writes, so a bot cannot lock itself out.
      settings = {
        model.default = config.hermesDeploy.model;

        # Native backend: commands run as the hermes user on the host.
        # The bot edits this flake and deploys, instead of mutating state.
        terminal.backend = "local";

        # High-value state: back up HERMES_HOME before any update.
        updates.pre_update_backup = "full";
      };

      # Tools the agent may invoke through its terminal toolset.
      # Keep this list intentional — everything here is exposed to the bot.
      extraPackages = with pkgs; [
        git
        curl
        jq
        ripgrep
        tailscale
      ];
    };

    services.tailscale = {
      enable = true;
      # Join the tailnet on first boot. The auth key is an agenix secret,
      # so it never appears in /nix/store or the flake repo.
      authKeyFile = "/run/agenix/tailscale-auth";
      extraUpFlags = [ "--ssh" ];
    };

    # Never put secrets in Nix config: values end up in /nix/store which is
    # world-readable. agenix decrypts them to /run/agenix only at activation.
    environment.systemPackages = [ pkgs.tailscale ];
  };
}
