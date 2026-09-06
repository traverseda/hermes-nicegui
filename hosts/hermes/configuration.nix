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
  # Bearer token the nicegui sessions/cron plugins send to the gateway
  # api_server (:8443). MUST equal the gateway's API_SERVER_KEY
  # (api-server-env). Without it every gateway call 401s.
  age.secrets."hermes-nicegui-gateway-token" = {
    file = ../../secrets/hermes-nicegui-gateway-token.age;
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
    gatewayTokenFile = config.age.secrets."hermes-nicegui-gateway-token".path;
    darkMode = true;
  };

  # ── Bot is root ──────────────────────────────────────────────────────
  # Hermes is a self-managing agent: give it root outright and let the
  # ROLLBACK machinery (git ledger + generations + health-checked watchdog)
  # be the safety net, not a permission gate. It is supposed to be hard for
  # the bot to kill itself, but if it really wants to it will — and the
  # operator's job is to keep the rollback path working, not to fence the bot
  # off. Layered least-privilege (polkit unit whitelists, etc.) just added
  # moving parts that broke in new ways and left the bot unable to fix
  # ordinary problems (e.g. rotating its own secrets).
  #
  # Grant hermes passwordless sudo DIRECTLY rather than via %wheel: NixOS 26.11
  # assigns wheel gid 1, which collides with the standard daemon group (also
  # gid 1), and sudo can't match %wheel reliably under that collision.
  security.sudo.extraRules = [
    {
      users = [ "hermes" ];
      commands = [
        {
          command = "ALL";
          options = [ "NOPASSWD" ];
        }
      ];
    }
  ];

  nix.settings = {
    experimental-features = [
      "nix-command"
      "flakes"
    ];
    # The bot is a trusted nix user: it can build/switch/roll back directly
    # (the hermes-deploy/hermes-rollback units remain the convenient wrappers,
    # but are no longer the only path). Every system change still lands as a
    # git commit + generation the watchdog can roll back.
    trusted-users = [
      "root"
      "@wheel"
      "hermes"
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
  # Operator access — without these, a fresh activation locks root out
  # (PermitRootLogin=prohibit-password + empty keys). Keep at least one
  # operator key here at all times.
  users.users.root.openssh.authorizedKeys.keys = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINqMoutqmTZsU+nMN5mdsJz+OnzvLtgDYFR/kOYW2YWU traverseda@azrael"
  ];
  # Pin root's shell to a literal store path instead of the default
  # /run/current-system/sw/bin/bash. The default only exists after a full NixOS
  # boot; during an in-place conversion (nixos-install --root /) the users
  # activation snippet writes it into /etc/passwd while still running Debian,
  # and since /run is a tmpfs that gets remounted, the path can vanish and lock
  # root out of both console and SSH. A literal store path (bash's own bin/bash)
  # exists as soon as the closure is present, and survives NixOS boots too.
  # NOTE: must be a plain string path, NOT a shell package — users-groups.nix
  # normalises any shell package back to /run/current-system/sw via toShellPath.
  users.users.root.shell = "${pkgs.bash}/bin/bash";

  # ── Networking ───────────────────────────────────────────────────────
  # Proxmox only wires the veth; the container configures eth0 itself
  # (unprivileged LXC: no host-side IP injection). The proxmox-lxc module
  # sets manageNetwork=false (networkd on, DHCP off), so without an explicit
  # eth0 networkd unit the container comes up with NO network → locked out.
  # This is a real-LXC bug the VM never caught.
  systemd.network.networks."10-eth0" = {
    matchConfig.Name = "eth0";
    networkConfig = {
      DHCP = "yes";
      LinkLocalAddressing = "no";
    };
    dhcpV4Config = {
      RouteMetric = 100;
    };
  };

  # ── Firewall ─────────────────────────────────────────────────────────
  # Tailscale traffic rides on the tailnet; nothing else needs exposing.
  networking.firewall = {
    enable = true;
    # ssh from the Proxmox host / tailnet
    allowedTCPPorts = [ 22 ];
  };

  # ── System ───────────────────────────────────────────────────────────
  system.stateVersion = "26.05";

  # configfs cannot be mounted in an unprivileged LXC, so the default
  # sys-kernel-config.mount fails at every boot/switch. switch-to-configuration
  # then returns non-zero, which makes EVERY nixos-rebuild switch "fail" —
  # breaking deploy.sh AND the bot's own hermes-deploy.service. Override the
  # unit to skip in containers (same behavior as the stock unit on a real
  # host). Nothing on this box needs configfs.
  systemd.units."sys-kernel-config.mount".text = ''
    [Unit]
    Description=Kernel Configuration File System
    Documentation=https://docs.kernel.org/filesystems/configfs.html
    Documentation=https://systemd.io/API_FILE_SYSTEMS
    DefaultDependencies=no
    ConditionVirtualization=!container
    ConditionPathExists=/sys/kernel/config
    ConditionCapability=CAP_SYS_RAWIO
    Before=sysinit.target
    Conflicts=umount.target
    Before=umount.target
    MounterImplicit=1

    [Mount]
    What=configfs
    Where=/sys/kernel/config
    Type=configfs
    Options=defaults
  '';

  # (The git-repo bootstrap for /var/lib/hermes-deploy lives in
  # modules/hermes-deploy.nix — hermes-deploy-repo — so it is shared with the
  # test VM and can't drift.)
}
