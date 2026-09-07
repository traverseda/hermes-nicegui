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
  hermes-agent ? null,
  ...
}:

let
  cfg = config.services.hermes-agent;
  llm = config.hermesDeploy.providers.${config.hermesDeploy.defaultProvider};
in
{

  config = lib.mkIf (config.hermesDeploy.defaultProvider != "") {
    services.hermes-agent = {
      enable = true;

      # Put the `hermes` CLI on PATH and export HERMES_HOME system-wide so
      # the interactive CLI shares state (sessions, skills, cron) with the
      # gateway service.
      addToSystemPackages = true;

      # Declarative config, deep-merged into config.yaml. Values here win
      # over anything the agent writes, so a bot cannot lock itself out.
      settings = {
        model.default = llm.model;
        model.provider = llm.provider;
        model.base_url = llm.baseUrl;
        model.api_key = "\${env:LLM_API_KEY}";

        # Agent clock: IANA timezone for hermes_time (session timestamps,
        # cron wall-clock anchoring). System TZ stays UTC; only the agent
        # operates on Halifax local time (t_03379e5a).
        timezone = "America/Halifax";

        # Native backend: commands run as the hermes user on the host.
        # The bot edits this flake and deploys, instead of mutating state.
        terminal.backend = "local";

        # High-value state: back up HERMES_HOME before any update.
        updates.pre_update_backup = "full";

        # Memory: built-in file-backed store (MEMORY.md/USER.md + `memory`
        # tool) is DISABLED in favour of the hindsight external memory
        # provider (modules/hindsight.nix; selected via provider below).
        # Disabling memory_enabled/user_profile_enabled drops the built-in
        # memory block from every system prompt (~800 tokens/turn) and gates
        # the background-review fork off memory writes; the hindsight provider
        # is loaded independently of these flags, so it keeps working.
        memory.provider = "hindsight";
        memory.memory_enabled = false;
        memory.user_profile_enabled = false;
        # Drop the built-in `memory` tool from every session's tool catalog
        # (agent.disabled_toolsets is the documented global-suppression knob,
        # subtracted last so it overrides all platform defaults). The store
        # behind it is disabled above; hindsight provider tools are injected
        # separately and are unaffected.
        agent.disabled_toolsets = [ "memory" ];

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
        # One worker at a time: cap concurrent tasks per profile so a burst
        # of tickets doesn't spawn a pile of parallel agents on this box.
        # Dispatcher reads this at gateway startup (kanban_watchers.py).
        kanban.max_in_progress_per_profile = 8;
        # Orchestrator toolset. `toolsets: [kanban]` is required for the
        # kanban tools' check_fn to pass in normal (non-worker) sessions —
        # the `all`/`*` wildcard deliberately does not enable kanban. The
        # api_server platform drops kanban from its default composite, so it
        # must be listed explicitly there too.
        toolsets = [ "kanban" ];
        platform_toolsets.api_server = [
          "hermes-api-server"
          "kanban"
        ];

        # ── Cost optimisation (ported from the old hermesagent.lan config,
        # t_472fe33a). The old box's cost posture was sound in principle but
        # its auto-compact was too aggressive for a ~1M-window model: it
        # capped full compaction at 128K tokens (~12% of the window) and
        # proactive-pruned stale tool outputs at 48K. Adapted here to the
        # same principles with saner triggers:
        #   - compression.threshold_tokens 262144: full compaction fires at
        #     ~256K tokens (25% of the 1M window) — bounds per-turn re-send
        #     cost without summarising away context the model can still use.
        #   - compression.proactive_prune_tokens 131072: deterministic
        #     no-LLM reclaim of old tool outputs on long sessions only, so
        #     stale file/terminal dumps stop being re-sent verbatim every
        #     turn (the old box's 48K trigger fired far too early).
        #   - agent.max_turns 150: cap worst-case loop spend (v0.20.1
        #     default is 500; the old box ran 150 and it was sufficient).
        # Compression config is read directly from config.yaml by
        # run_agent.py per session, so no gateway restart trigger is needed.
        agent.max_turns = 150;
        compression.threshold_tokens = 262144;
        compression.proactive_prune_tokens = 131072;
      };

      # AgentMail MCP server — gives the agent its own inbox (send/receive
      # email). The API key is NOT declared here: it lives in the hermes-env
      # agenix secret (.env) and is referenced as `${env:AGENTMAIL_API_KEY}`,
      # which hermes resolves from the loaded .env at runtime (mcp_tool.py
      # `_env_ref_name`). Putting the literal key here would leak it into
      # config.yaml → the world-readable nix store.
      mcpServers.agentmail = {
        command = "npx";
        args = [
          "-y"
          "agentmail-mcp"
        ];
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
        zerotierone
        # nodejs provides `npx`, required by the agentmail MCP server
        # (npx -y agentmail-mcp). Keep in sync with mcpServers.agentmail.
        nodejs
        # Secret rotation: age decrypts/encrypts the agenix *.age files with
        # the LXC host key (/etc/ssh/ssh_host_ed25519_key). The bot needs this
        # to fix its own secrets (e.g. the CHANGE-ME placeholder credentials).
        age
        # uv: fast Python package/env manager — generally useful for
        # debugging and testing (venv creation, ephemeral tool installs).
        # The box's system python3 is PEP 668 (no pip), so uv is the
        # agent's clean lane for throwaway python environments.
        uv
      ];
    };

    services.tailscale = {
      enable = true;
      # Join the tailnet on first boot. The auth key is an agenix secret,
      # so it never appears in /nix/store or the flake repo.
      authKeyFile = "/run/agenix/tailscale-auth";
      extraUpFlags = [ "--ssh" ];
    };

    # ZeroTier One: gives the LXC access to the 192.168.193.x network where
    # vLLM runs (192.168.193.96:8000). Without this the LXC cannot reach the
    # local model and hermes-agent falls back to OpenRouter (400).
    services.zerotierone = {
      enable = true;
      joinNetworks = [ "68bea79acfbb542c" ];
    };

    # ── The bot is root — the service sandbox must not veto it ─────────
    # hosts/hermes/configuration.nix gives the hermes user passwordless sudo
    # and nix trusted-user. The vendored hermes-agent module hardens the
    # service with NoNewPrivileges + ProtectSystem=strict, which makes that
    # grant useless: NoNewPrivileges blocks sudo/setuid at the kernel level
    # (the bot process shows NoNewPrivs: 1) and ProtectSystem=strict freezes
    # /etc,/usr,/boot for every process in the unit, root included. Relax both
    # so the bot's root is real root — the safety net is the rollback
    # machinery, not a kernel sandbox.
    # OnFailure: if the agent crashes, trigger content recovery which can
    # revert skills/tools/MCP registrations at content-lane granularity —
    # no Nix rebuild needed. If that doesn't fix it, the chain escalates
    # to a Nix generation rollback via hermes-rollback-run.service.
    # Both hermes-agent and hermes-nicegui share this OnFailure chain.
    systemd.services.hermes-agent = {
      after = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];
      serviceConfig = {
        NoNewPrivileges = lib.mkForce false;
        ProtectSystem = lib.mkForce false;
        # OnFailure: agent crash triggers content recovery → content recovery
        # failure escalates to Nix generation rollback.
        OnFailure = [ "hermes-content-recovery-run.service" ];
      };
      # Restart the gateway when these markers change between generations.
      # Bumped for the Discord token migration (t_c5b70744): the deploy that
      # first activates the new settings/secret must restart the gateway so
      # it loads the new config.yaml/.env (platform enablement is read at
      # startup only). Leave the marker; bump it for any future change that
      # must take effect on deploy.
      restartTriggers = [
        "discord-migration-2026-08-15"
        "timezone-halifax-2026-08-15"
        "selfkill-fix-verify-2026-08-15"
        # opencode hindsight memory: HINDSIGHT_API_TOKEN added to hermes-env,
        # HINDSIGHT_BANK_ID removed (was forcing opencode off the code bank);
        # gateway must reload .env so opencode children authenticate.
        "hindsight-opencode-memory-2026-08-16"
        # Default model switch to QuantTrio/Qwen3.6-35B-A3B-AWQ (R4 operator
        # mandate t_89f84f69; hermesDeploy.model); gateway must reload
        # config.yaml so new sessions start on the new model.
        "default-model-Qwen3.6-35B-A3B-AWQ-2026-08-26"
      ];
      # The module's PATH only has the agent's extraPackages. Expose the
      # setuid sudo wrapper (/run/wrappers/bin) and the system tools
      # (/run/current-system/sw/bin) so the bot can actually sudo.
      path = [
        "/run/wrappers"
        "/run/current-system/sw"
      ];
    };

    # Never put secrets in Nix config: values end up in /nix/store which is
    # world-readable. agenix decrypts them to /run/agenix only at activation.
    environment.systemPackages = [
      pkgs.tailscale
      # age on the system PATH too (the agent extraPackages above cover the
      # gateway PATH; this covers interactive hermes shells / sudo).
      pkgs.age
    ];
  };
}
