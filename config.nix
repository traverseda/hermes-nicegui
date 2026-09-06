# Central Nix module for LLM provider configuration.
#
# This is the single declarative source of truth for all LLM providers,
# models, and endpoints. Non-secret config (URLs, models, providers) lives
# here — diffable, versioned, visible. API keys come from the encrypted
# /run/agenix/config env file.
#
# Usage in configuration.nix:
#   hermesDeploy.providers = {
#     local = {
#       name = "local"; provider = "vllm"; model = "quanttrio/...";
#       baseUrl = "http://...";
#     };
#     openrouter = {
#       name = "openrouter"; provider = "openrouter"; model = "google/gemini-2.5-flash";
#     };
#   };
#   hermesDeploy.defaultProvider = "local";
#   hermesDeploy.fallbackProviders = [ "openrouter" ];
#
# Consumers: hermes-service.nix, hindsight.nix, opencode.nix
#

{ config, lib, ... }:

let
  cfg = config.hermesDeploy;
in
{
  options.hermesDeploy.providers = lib.mkOption {
    type    = lib.types.attrsOf (
      lib.types.submodule {
        options = {
          name    = lib.mkOption { type = lib.types.str; description = "Provider display name."; };
          provider= lib.mkOption { type = lib.types.str; description = "Built-in provider name or 'custom'."; };
          model   = lib.mkOption { type = lib.types.str; description = "Model identifier."; };
          baseUrl = lib.mkOption {
            type    = lib.types.nullOr lib.types.str;
            default = null;
            description = "OpenAI-compatible endpoint URL (required when provider='custom').";
          };
          models  = lib.mkOption {
            type    = lib.types.listOf lib.types.str;
            default = lib.optionals (cfg.defaultProvider != "") [ cfg.providers.${cfg.defaultProvider}.model ];
            description = "Model list for opencode provider configs.";
          };
        };
      }
    );
    default = { };
    description = "Named LLM provider definitions.";
  };

  options.hermesDeploy.defaultProvider = lib.mkOption {
    type    = lib.types.str;
    default = "";
    description = "Name of the default provider (key into providers).";
  };

  options.hermesDeploy.fallbackProviders = lib.mkOption {
    type    = lib.types.listOf lib.types.str;
    default = [ ];
    description = "Ordered list of provider names to try if the default fails.";
  };

  options.hermesDeploy.hindsight = lib.mkOption {
    type    = lib.types.submodule {
      options.provider = lib.mkOption {
        type    = lib.types.str;
        default = "openai";
        description = "LLM provider name as understood by the Hindsight container. Defaults to 'openai' (vLLM is OpenAI-compatible).";
      };
      options.port = lib.mkOption { type = lib.types.int; default = 8888; description = "Hindsight API port."; };
    };
    default   = { port = 8888; };
    description = "Hindsight service options.";
  };

  # Back-compat shim: export a single "llm" object for consumers that
  # expect `config.hermesDeploy.llm` (hermes-service.nix still uses this).
  options.hermesDeploy.llm = lib.mkOption {
    type    = lib.types.attrs;
    readOnly = true;
    internal = true;
    default = lib.optionalAttrs (cfg.defaultProvider != "") {
      name    = cfg.providers.${cfg.defaultProvider}.name;
      provider= cfg.providers.${cfg.defaultProvider}.provider;
      model   = cfg.providers.${cfg.defaultProvider}.model;
      baseUrl = cfg.providers.${cfg.defaultProvider}.baseUrl;
    };
    description = "Computed default-provider config (back-compat shim, not for direct use).";
  };

  config = lib.mkIf (cfg.defaultProvider != "") {
    assertions = [
      {
        assertion = builtins.hasAttr cfg.defaultProvider cfg.providers;
        message   = "hermesDeploy.defaultProvider '${cfg.defaultProvider}' not in hermesDeploy.providers.";
      }
      {
        assertion = lib.all (p: builtins.hasAttr p cfg.providers) cfg.fallbackProviders;
        message   = "One or more hermesDeploy.fallbackProviders not in hermesDeploy.providers.";
      }
    ];
  };
}
