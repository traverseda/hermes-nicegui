# Hermes LXC configuration — runs on Proxmox VE.
#
# Importing the proxmox-lxc module sets up:
#   * boot.isContainer = true (no kernel/bootloader — the host provides it)
#   * systemd-networkd to accept network config from Proxmox
#   * sshd on demand
#   * a tarball build (`system.build.tarball`) for importing into Proxmox
#
# Use `scripts/import-proxmox.sh` to build and import this into a CT.

{
  config,
  pkgs,
  lib,
  modulesPath,
  ...
}:

{
  imports = [
    (modulesPath + "/virtualisation/proxmox-lxc.nix")
  ];

  proxmoxLXC = {
    enable = true;
    # Unprivileged LXC: network & hostname are managed by Proxmox.
    manageNetwork = false;
    manageHostName = false;
  };

  # The hermes service itself lives in modules/hermes-service.nix (shared
  # with the test VM). Overrides that are LXC-specific:
  services.hermes-agent = {
    stateDir = "/var/lib/hermes";
    workingDirectory = "/var/lib/hermes/workspace";

    # Official Hindsight memory provider (bundled with hermes-agent; selected
    # via the `memory.provider` config key). Points at the local Hindsight
    # service below. The API key lives in the hermes-env secret
    # (HINDSIGHT_API_KEY = the same value as HINDSIGHT_API_TENANT_API_KEY).
    settings.memory.provider = "hindsight";
    environment = {
      HINDSIGHT_MODE = "cloud";
      HINDSIGHT_API_URL = "http://127.0.0.1:8888";
      HINDSIGHT_BANK_ID = "hermes";
    };
  };

  # Git-driven deploy + health-checked auto-rollback (see module docs).
  services.hermes-deploy = {
    enable = true;
    repoDir = "/var/lib/hermes-deploy";
    flakeAttr = "hermes";
    branch = "main";
  };

  # Content-store fast path: agent-authored skills/tools/MCP servers live in a
  # git-backed store (recovered by `hermes-tool revert`) instead of a Nix
  # rebuild for every new tool. The deploy watchdog tries this BEFORE a
  # generation rollback. See modules/hermes-tools.nix.
  services.hermes-tools = {
    enable = true;
  };

  # ── Hindsight memory service ─────────────────────────────────────────
  # Standalone agent-memory API (retain/recall/reflect), tailnet-only.
  # Model comes from `hermesDeploy.llm` (defaults to opencode-go /
  # deepseek-v4-flash); secrets come from the agenix env file below.
  # Per-bank split: only the `code` bank (opencode) gets byte-exact verbatim
  # retain + engineering missions; the `hermes` bank (the agent's general
  # chat) stays on untouched defaults.
  services.hindsight = {
    enable = true;
    environmentFile = config.age.secrets."hindsight-env".path;
    codeBanks = [ "code" ];
  };

  # ── Secrets (agenix) ─────────────────────────────────────────────────
  # Encrypted with your pubkey (see secrets/README.md). Decrypted to
  # /run/agenix/... at activation time — never in /nix/store.
  age.secrets."hermes-env" = {
    file = ../../secrets/hermes-env.age;
    owner = "hermes";
    group = "hermes";
    mode = "0440";
  };
  age.secrets."api-server-env" = {
    file = ../../secrets/api-server-env.age;
    owner = "hermes";
    group = "hermes";
    mode = "0440";
  };
  age.secrets."tailscale-auth" = {
    file = ../../secrets/tailscale-auth.age;
    owner = "root";
    mode = "0400";
  };
  age.secrets."hindsight-env" = {
    file = ../../secrets/hindsight-env.age;
    owner = "root";
    mode = "0440";
  };
  age.secrets."dashboard-env" = {
    file = ../../secrets/dashboard-env.age;
    owner = "hermes";
    group = "hermes";
    mode = "0440";
  };
  age.secrets."cloudflare-tunnel" = {
    file = ../../secrets/cloudflare-tunnel.age;
    owner = "root";
    mode = "0400";
  };
  age.secrets."xaelwiki-env" = {
    file = ../../secrets/xaelwiki-env.age;
    owner = "xaelwiki";
    group = "xaelwiki";
    mode = "0440";
  };
  age.secrets."xaelwiki-ssh" = {
    file = ../../secrets/xaelwiki-ssh.age;
    owner = "xaelwiki";
    group = "xaelwiki";
    mode = "0440";
  };
  age.secrets."hermes-nicegui-env" = {
    file = ../../secrets/hermes-nicegui-env.age;
    owner = "hermes";
    group = "hermes";
    mode = "0440";
  };

  services.hermes-agent.environmentFiles = [
    config.age.secrets."hermes-env".path
  ];

  # ── Home Assistant API server ────────────────────────────────────────
  # The main gateway multiplexes the `ha` profile under /p/ha/ on its api_server
  # (127.0.0.1:8443); hermes-ha-proxy.service terminates :8444 and forwards there
  # (streaming, SSE-safe) so Home Assistant talks to a plain OpenAI-compatible
  # endpoint on hermesagent.lan:8444. API_SERVER_KEY (the listener key) comes
  # from the api-server-env agenix secret and is appended to the default
  # profile's .env by the module.
  services.hermes-ha = {
    enable = true;
    apiServerKeyFile = config.age.secrets."api-server-env".path;
  };

  # ── Hermes web dashboard ─────────────────────────────────────────────
  # Web admin panel (`hermes dashboard`), exposed on hermesagent.lan:9119
  # over the tailnet only. Binding 0.0.0.0 engages the dashboard's own auth
  # gate; the `basic` (username/password) provider reads its credentials
  # from the dashboard-env agenix secret, appended to the default profile's
  # .env — same pattern as the API_SERVER_KEY / hindsight keys.
  services.hermes-dashboard = {
    enable = true;
    environmentFile = config.age.secrets."dashboard-env".path;
  };

  # ── Cloudflare Tunnel ───────────────────────────────────────────────
  # Public exposure for selected services, outbound-only (cloudflared dials
  # out to Cloudflare; no inbound port is opened). REMOTELY-managed tunnel:
  # the tunnel + public hostnames are configured in the Cloudflare dashboard,
  # and the LXC runs `cloudflared tunnel run --token` with the connector token
  # (cfut_…) from the agenix secret. Publishing a hostname is a dashboard
  # action, NOT a flake edit — nothing is publicly routable until you add
  # hostnames for this tunnel in the dashboard.
  services.cloudflare-tunnel = {
    enable = true;
    tokenFile = config.age.secrets."cloudflare-tunnel".path;
  };

  # ── xaelwiki notes MCP server ────────────────────────────────────────
  # Editable, low-stakes, independently revertible. Source runs live from the
  # vendor/xaelWiki submodule (symlinked into the run location); notes vault is
  # a git clone of codeberg notes.git via the xaelwiki SSH deploy key. The MCP
  # bearer token comes from xaelwiki-env; the deploy key from xaelwiki-ssh.
  services.xaelwiki = {
    enable = true;
    environmentFile = config.age.secrets."xaelwiki-env".path;
    sshKeyFile = config.age.secrets."xaelwiki-ssh".path;
  };

  # ── hermes-nicegui web UI ────────────────────────────────────────────
  # Modular NiceGUI browser UI (profile switcher, sessions, cron, kanban,
  # terminal, files). Runs as the hermes user sharing $HERMES_HOME so the CLI
  # subprocess + profile switcher see the real agent state. Binds loopback and
  # is reached PUBLICLY through the Cloudflare tunnel: add a public hostname
  # in the Cloudflare dashboard pointing at http://localhost:8080 (a dashboard
  # action, not a flake edit). The app's own login is the auth gate; the
  # kanban plugin logs into the hermes dashboard with the PLAINTEXT
  # username/password from the hermes-nicegui-env agenix secret (the
  # dashboard-env secret only has the scrypt hash).
  services.hermes-nicegui = {
    enable = true;
    environmentFile = config.age.secrets."hermes-nicegui-env".path;
  };

  # ── Nix ──────────────────────────────────────────────────────────────
  nix.settings = {
    experimental-features = [
      "nix-command"
      "flakes"
    ];
    # Let the bot (via hermes user) and root trigger builds/rollbacks.
    trusted-users = [
      "root"
      "@wheel"
    ];
    substituters = [ "https://cache.nixos.org" ];
    trusted-public-keys = [ "cache.nixos.org-1:6NCHdD59X431o0gWypbMrWURtJAfVcLI/QkGjRcUv6w=" ];
  };

  # ── SSH ──────────────────────────────────────────────────────────────
  # Deploy from a build machine via: nixos-rebuild switch --target-host ...
  services.openssh = {
    enable = true;
    settings = {
      PermitRootLogin = "prohibit-password";
      PasswordAuthentication = false;
    };
  };
  # Populate with the operator's pubkeys:  ssh-keyscan / add manually.
  users.users.root.openssh.authorizedKeys.keys = [
    # "ssh-ed25519 AAAA... operator@machine"
  ];

  # ── Firewall ─────────────────────────────────────────────────────────
  # Tailscale traffic rides on the tailnet; nothing else needs exposing.
  networking.firewall = {
    enable = true;
    # ssh from the Proxmox host / tailnet
    allowedTCPPorts = [ 22 ];
  };

  # ── System ───────────────────────────────────────────────────────────
  system.stateVersion = "26.05";

  system.activationScripts.hermes-repo-init = lib.stringAfter [ "users" ] ''
    # The deploy machinery expects a git checkout of this flake here.
    mkdir -p /var/lib/hermes-deploy
    chmod 0755 /var/lib/hermes-deploy
    if [ ! -d /var/lib/hermes-deploy/.git ]; then
      echo "hermes: /var/lib/hermes-deploy is not a git repo."
      echo "hermes: clone it after first boot:"
      echo "  git clone <this-repo> /var/lib/hermes-deploy"
    fi
  '';
}
