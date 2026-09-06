# Opencode CLI installed and configured from this flake.
#
# Installs the opencode binary and writes its global config to
# /var/lib/hermes/.config/opencode/opencode.jsonc on every activation.
# Config is generated from the shared hermesDeploy.llm config so there's
# one flag to change the model + provider + endpoint for all services
# (hermes, hindsight, opencode).

{ config, lib, pkgs, ... }:

let
  cfg = config.services.opencode;
  llm = config.hermesDeploy.llm;

  # Opencode expects provider/model in the model field.
  opencodeModel = "${llm.provider}/${llm.model}";

  # Generate the opencode global config from shared llm defaults.
  # This is the same for all hosts — one source of truth.
  opencodeConfigJson = pkgs.writeText "opencode-global.jsonc"
    (builtins.toJSON {
      "$schema" = "https://opencode.ai/config.json";
      model = opencodeModel;
      small_model = opencodeModel;
      plugin = [
        [
          "@vectorize-io/opencode-hindsight"
          {
            hindsightApiUrl = "http://127.0.0.1:${toString cfg.hindsightPort}";
            bankId = cfg.hindsightBankId;
            retainTags = cfg.hindsightPluginTags;
            retainMetadata = { repo = "{gitProject}"; };
          }
        ]
      ];
    });
in
{
  options.services.opencode = {
    enable = lib.mkEnableOption "opencode CLI and global config";

    # Override the model used by opencode (defaults to shared llm config).
    model = lib.mkOption {
      type = lib.types.str;
      default = opencodeModel;
      description = ''
        Model for opencode.  Defaults to the shared hermesDeploy.llm config
        (provider/model).  Override to use a different provider + model pair.
      '';
    };

    hindsightPort = lib.mkOption {
      type = lib.types.port;
      default = 8888;
      description = "Port of the local Hindsight API (used by the plugin).";
    };

    hindsightBankId = lib.mkOption {
      type = lib.types.str;
      default = "code";
      description = "Hindsight bank used by the opencode hindsight plugin.";
    };

    hindsightPluginTags = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "project:{gitProject}" "env:work" ];
      description = "retainTags passed to the opencode-hindsight plugin.";
    };
  };

  config = lib.mkIf cfg.enable {
    # ── binary ────────────────────────────────────────────────────────────
    environment.systemPackages = with pkgs; [ opencode ];

    # ── global config (hermes user) ───────────────────────────────────────
    # Writes ~/.config/opencode/opencode.jsonc to the hermes user's
    # HOME on every activation. This is a repo-agnostic config: every
    # opencode session uses the same model/provider/endpoint, regardless
    # of working tree or whether the flake repo is present.
    system.activationScripts.opencode-config = lib.mkIf config.services.hermes-agent.enable {
      deps = [ "users" ];
      text = ''
        mkdir -p /var/lib/hermes/.config/opencode
        install -m 0640 -o hermes -g hermes \
          ${opencodeConfigJson} \
          /var/lib/hermes/.config/opencode/opencode.jsonc
      '';
    };
  };
}
