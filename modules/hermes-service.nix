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
      default = "deepseek-v4-flash";
      description = "Default model identifier for Hermes.";
    };

    # Inference provider for the default model (hermes_cli.auth
    # PROVIDER_REGISTRY id). Defaults to opencode-go, matching the operator's
    # own assistant model; key lives in hermes-env as OPENCODE_GO_API_KEY.
    provider = lib.mkOption {
      type = lib.types.str;
      default = "opencode-go";
      description = "Inference provider id for the default model.";
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
        model.provider = config.hermesDeploy.provider;

        # Native backend: commands run as the hermes user on the host.
        # The bot edits this flake and deploys, instead of mutating state.
        terminal.backend = "local";

        # High-value state: back up HERMES_HOME before any update.
        updates.pre_update_backup = "full";

        # ── Kanban ticketing ────────────────────────────────────────────
        # The default profile is the work executor for tickets filed by the
        # `ha` profile (and by itself). The dispatcher runs inside the gateway
        # (kanban.dispatch_in_gateway). Triage tasks are NOT auto-decomposed,
        # so nothing fans out into surprise LLM work; tasks only run when an
        # agent files them with an explicit assignee (or you triage them).
        # auto_subscribe_on_create wakes the ticket's origin session when the
        # task reaches a terminal event, so the ha profile learns the outcome.
        kanban.dispatch_in_gateway = true;
        kanban.auto_decompose = false;
        kanban.auto_subscribe_on_create = true;
        # Orchestrator toolset. `toolsets: [kanban]` is required for the
        # kanban tools' check_fn to pass in normal (non-worker) sessions —
        # the `all`/`*` wildcard deliberately does not enable kanban. The
        # api_server platform drops kanban from its default composite, so it
        # must be listed explicitly there too.
        toolsets = [ "kanban" ];
        platform_toolsets.api_server = [ "hermes-api-server" "kanban" ];
      };

      # AgentMail MCP server — gives the agent its own inbox (send/receive
      # email). The API key is NOT declared here: it lives in the hermes-env
      # agenix secret (.env) and is referenced as `${env:AGENTMAIL_API_KEY}`,
      # which hermes resolves from the loaded .env at runtime (mcp_tool.py
      # `_env_ref_name`). Putting the literal key here would leak it into
      # config.yaml → the world-readable nix store.
      mcpServers.agentmail = {
        command = "npx";
        args = [ "-y" "agentmail-mcp" ];
        env.AGENTMAIL_API_KEY = "\${env:AGENTMAIL_API_KEY}";
      };

      # Workspace policy: always-on "file a ticket instead" rules for the
      # default profile's own sessions (workers get separate kanban guidance
      # injected by the dispatcher + the `ticketing` skill on their task).
      documents = {
        ".hermes.md" = builtins.readFile ./ticketing/default-hermes.md;
      };

      # Tools the agent may invoke through its terminal toolset.
      # Keep this list intentional — everything here is exposed to the bot.
      extraPackages = with pkgs; [
        git
        curl
        jq
        ripgrep
        tailscale
        # nodejs provides `npx`, required by the agentmail MCP server
        # (npx -y agentmail-mcp). Keep in sync with mcpServers.agentmail.
        nodejs
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
