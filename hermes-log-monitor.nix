# hermes-log-monitor — NixOS systemd service module
#
# Watches hermes-agent, hermes-nicegui, and podman-hindsight log files
# for errors and opens deduplicated kanban tickets.
#
# Usage: set services.hermes-log-monitor.enable = true in config.nix

{ config, lib, pkgs, ... }:

with lib;

let
  cfg = config.services.hermes-log-monitor;
in

{
  options.services.hermes-log-monitor = {
    enable = mkEnableOption "hermes-log-monitor service";
  };

  config = mkIf cfg.enable {
    systemd.services.hermes-log-monitor = {
      description = "Hermes Log Monitor — watches service logs for errors and files kanban tickets";
      wantedBy = [ "multi-user.target" ];
      requires = [ "hermes-agent.service" ];
      after = [ "hermes-agent.service" ];

      script = ''
        ${pkgs.python3}/bin/python ${./hermes-tooling/log-monitor.py}
      '';

      serviceConfig = {
        Type = "simple";
        User = "hermes";
        Group = "hermes";
        WorkingDirectory = "/var/lib/hermes/workspace";
        Environment = "HERMES_HOME=/var/lib/hermes/.hermes";
        StandardOutput = "journal";
        StandardError = "journal";
        Restart = "on-failure";
        RestartSec = "10";
        TimeoutStartSec = "30";
        ReadWritePaths = "/var/lib/hermes";
      };
    };
  };
}
