# Generic "preferred model" configuration shared by services that need an LLM
# (currently Hindsight's retain/recall/reflect memory extraction).
#
# Keep this as the single place to set which model services should prefer.
# Defaults to the local/custom vLLM endpoint (http://192.168.193.96:8000/v1)
# with quanttrio/Qwen3.6-35B-A3B-AWQ.
# The API key never lives in Nix config — it stays in the service's agenix env
# file, under the variable named by `apiKeyEnvVar`.

{ lib, ... }:

{
  options.hermesDeploy.llm = {
    provider = lib.mkOption {
      type = lib.types.str;
      default = "vllm";
      description = "Provider identifier understood by the consuming service (OpenAI-compatible).";
    };

    baseUrl = lib.mkOption {
      type = lib.types.str;
      default = "http://192.168.193.96:8000/v1";
      description = "OpenAI-compatible base URL of the preferred model endpoint.";
    };

    model = lib.mkOption {
      type = lib.types.str;
      default = "quanttrio/Qwen3.6-35b-a3b-awq";
      description = "Preferred model identifier.";
    };

    apiKeyEnvVar = lib.mkOption {
      type = lib.types.str;
      default = "HINDSIGHT_API_LLM_API_KEY";
      description = ''
        Name of the environment variable, inside the service's agenix env file,
        that carries the API key for the preferred model.
      '';
    };
  };
}
