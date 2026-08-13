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

  # ── Hindsight memory service ─────────────────────────────────────────
  # Standalone agent-memory API (retain/recall/reflect), tailnet-only.
  # Model comes from `hermesDeploy.llm` (defaults to opencode-go /
  # deepseek-v4-flash); secrets come from the agenix env file below.
  # `hermes` is a code bank: verbatim retain + engineering-focused mission,
  # applied via the per-bank config API (others stay neutral — the service
  # also hosts non-code banks).
  services.hindsight = {
    enable = true;
    environmentFile = config.age.secrets."hindsight-env".path;
    codeBanks = [ "hermes" ];
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
  age.secrets."home-assistant-env" = {
    file = ../../secrets/home-assistant-env.age;
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

  services.hermes-agent.environmentFiles = [
    config.age.secrets."hermes-env".path
  ];

  # ── Second gateway: OpenAI-compatible API server (home-assistant profile) ──
  # Runs `hermes gateway` against the `home-assistant` profile on its own
  # port (default 8643), so Home Assistant / other clients can hit it via the
  # OpenAI-compatible API without touching the main agent's state.
  # API_SERVER_KEY comes from the home-assistant-env agenix secret.
  services.hermes-home-assistant = {
    enable = true;
    environmentFile = config.age.secrets."home-assistant-env".path;
    # To reach it from elsewhere on the tailnet:
    #   host = "0.0.0.0";
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
