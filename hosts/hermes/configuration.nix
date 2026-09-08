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
    ../../modules/config.nix
    ../../modules/opencode.nix
  ];

  proxmoxLXC = {
    enable = true;
    # Unprivileged LXC: network & hostname are managed by Proxmox.
    manageNetwork = false;
    manageHostName = false;
  };

  # Bind-mount the deploy repo into the agent workspace so the nicegui Files
  # tab (sandboxed to HERMES_FILES_ROOT=/var/lib/hermes/workspace) can
  # browse/edit the flake and source code. Bind mounts are invisible to path
  # resolution, so the files-plugin sandbox check passes while the Files tab
  # shows the SAME live tree — no copy, no divergence, and the
  # deploy/rollback/watchdog pipeline is untouched. Browser edits land
  # as uncommitted changes in the repo (they still need a commit +
  # nixos-rebuild to ship). Nothing about this mount technically restricts
  # edits to flake inputs alone (hermes-agent, hermes-nicegui, xaelWiki are
  # github: inputs now — there is no vendor/ directory; the deploy pre-check
  # verifies this on every deploy); the whole repoDir, including flake.nix and hosts/,
  # is reachable this way. That's a deliberate tradeoff, not an oversight —
  # the Files tab sits behind the nicegui app's own login either way, and the
  # git ledger + generation rollback are what make any edit through it
  # reversible, same as an edit made over ssh.
  fileSystems."/var/lib/hermes/workspace/hermes-deploy" = {
    device = "/var/lib/hermes-deploy";
    fsType = "none";
    options = [ "bind" ];
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
    # HINDSIGHT_BANK_ID is deliberately NOT set: the hermes provider defaults
    # to the "hermes" bank on its own, and the opencode hindsight plugin
    # (global opencode config) would otherwise inherit this env var and be
    # forced off its own `bankId: "code"` — env overrides beat plugin options.
    # HINDSIGHT_API_TOKEN (same tenant key, added to hermes-env 2026-08-16)
    # authenticates the opencode plugin against the local API.
    settings.memory.provider = "hindsight";
    environment = {
      HINDSIGHT_MODE = "cloud";
      HINDSIGHT_API_URL = "http://127.0.0.1:8888";
    };

    # Operator mandate t_89f84f69 (2026-08-25): reliability-first free window.
    # Declared here so the activation deep-merge keeps the live kanban
    # concurrency instead of reverting it to the module default (1).
    settings.kanban.max_in_progress_per_profile = 4;
  };

  # Git-driven deploy + health-checked auto-rollback (see module docs).
  services.hermes-deploy = {
    enable = true;
    repoDir = "/var/lib/hermes-deploy";
    flakeAttr = "hermes";
    branch = "main";
    # Enable review gate: prevents unreviewed commits from being deployed.
    # The operator must apply a "reviewed" tag or "Reviewed-by:" trailer
    # before the deploy proceeds.  See docs/review-gate.md for the workflow.
    requireReview = true;
    # R3 (ticket t_012f76dd) e2e demo: register a scratch file so the
    # activationFiles mechanism is exercised during the real deploy cycle.
    activationFiles = {
      r3-demo = {
        source = ./r3-activation-scratch.txt;
        dest = "/var/lib/hermes/.hermes/test/r3-demo.txt";
        mode = "0644";
        owner = "hermes";
        group = "hermes";
      };
    };
  };

  # ── Opencode handled by modules/opencode.nix ─────────────────────
  # The opencode module manages the binary and generates its global config
  # from hermesDeploy.providers. The old activation script above is removed.

  # Content-store fast path: agent-authored skills/tools/MCP servers live in a
  # git-backed store (recovered by `hermes-tool revert`) instead of a Nix
  # rebuild for every new tool. The deploy watchdog tries this BEFORE a
  # generation rollback. See modules/hermes-tools.nix.
  services.hermes-tools = {
    enable = true;
  };

  # ── Hindsight memory service ─────────────────────────────────────────
  # Standalone agent-memory API (retain/recall/reflect), tailnet-only.
  # Model comes from `hermesDeploy.providers` (defaults to vllm /
  # quanttrio/Qwen3.6-35B-A3B-AWQ); secrets come from the agenix env file below.
  # Per-bank split: only the `code` bank (opencode) gets byte-exact verbatim
  # retain + engineering missions; the `hermes` bank (the agent's general
  # chat) stays on untouched defaults.
  services.hindsight = {
    enable = true;
    environmentFile = "/run/agenix/hindsight.env";
    codeBanks = [ "code" ];
    # Next.js control-plane web UI (:9999), tailnet-only behind the
    # HINDSIGHT_CP_ACCESS_KEY login (key already present in config).
    # The exposure registry opens :9999 on tailscale0 automatically.
    enableControlPlane = true;
  };

  # ── Secrets (agenix, one file per secret) ─────────────────────────────
  # Encrypted with two recipients (operator + LXC host key, see
  # secrets/README.md). Each secret is its own secrets/<NAME>.age file,
  # declared by modules/hermes-secrets.nix from secrets/manifest and
  # decrypted to /run/agenix/<NAME> at activation (never /nix/store).
  # That module also assembles per-consumer aggregate env files
  # (/run/agenix/hermes.env, /run/agenix/hindsight.env, …) AFTER agenix has
  # decrypted and BEFORE the hermes-agent .env build, so every consumer
  # below just points at its aggregate.
  services.hermes-secrets.enable = true;

  # Tailscale auth key: separate, operator-provisioned (minted in Tailscale
  # admin console). Consumed directly as authKeyFile, not via an env file.
  age.secrets."tailscale-auth" = {
    file = ../../secrets/tailscale-auth.age;
    owner = "root";
    mode = "0400";
  };
  # Cloudflare tunnel token: separate file, operator-provisioned in
  # Cloudflare Zero Trust dashboard.
  age.secrets."cloudflare-tunnel" = {
    file = ../../secrets/cloudflare-tunnel.age;
    owner = "root";
    mode = "0400";
  };
  # Xaelwiki SSH deploy key: separate file, operator-managed on Codeberg.
  age.secrets."xaelwiki-ssh" = {
    file = ../../secrets/xaelwiki-ssh.age;
    owner = "xaelwiki";
    group = "xaelwiki";
    mode = "0440";
  };

  # ── Home Assistant API server ────────────────────────────────────────
  # The main gateway multiplexes the `ha` profile under /p/ha/ on its api_server
  # (127.0.0.1:8443); hermes-ha-proxy.service terminates :8444 and forwards there
  # (streaming, SSE-safe) so Home Assistant talks to a plain OpenAI-compatible
  #   endpoint on hermesagent.lan:8444. API_SERVER_KEY (the listener key) is
  #   part of the hermes-secrets hermes.env aggregate, which the module builds
  #   the default profile's .env from.
  services.hermes-ha = {
    enable = true;
    apiServerKeyFile = "/run/agenix/hermes.env";
    # Carried over from the old box's ha profile (hermesagent.lan), adapted:
    # mcp_servers.fixups is deliberately NOT migrated — the ha profile's
    # escalation is now kanban (see modules/ticketing/ha-hermes.md), not the
    # Discord fixups forum. Skills lock-down via skills.external_dirs = [] +
    # profile-local skills dir, matching the old profile's curated set.
    settings = {
      agent = {
        max_turns = 150;
        reasoning_effort = "minimal";
        gateway_timeout = 1800;
      };
      platforms.homeassistant = {
        enabled = true;
        extra = {
          watch_entities = [ "input_text.hermes_command" ];
          cooldown_seconds = 5;
        };
      };
      platform_toolsets.homeassistant = [
        "homeassistant"
        "web"
        "tts"
        "skills"
        "clarify"
      ];
      skills.external_dirs = [ ];
      tts.provider = "edge";
      tts.edge.voice = "en-US-AriaNeural";
      stt.enabled = true;
      stt.provider = "local";
      stt.local.model = "base";
      mcp_servers.xaelwiki = {
        url = "http://127.0.0.1:8000/mcp";
        headers.Authorization = "Bearer \${env:XAEL_AUTH_TOKEN}";
      };
    };
  };

  # ── Home Assistant platform (inbound, gateway-level) ────────────────
  # HA events/voice reach the agent through the gateway's homeassistant
  # platform and route to the ha profile (ha-voice route). Credentials
  # come from hass-env via environmentFiles above.
  # Only the ha profile serves homeassistant — the default profile shares
  # the same credentials and cannot (gateway rejects duplicate credentials).
  services.hermes-agent.settings = {
    platforms.homeassistant.enabled = false;
    # Discord platform (migrated 2026-08-15 from the deprecated bot on
    # hermes@hermesagent.lan). Token + allowed users + home channel live in
    # the hermes-env agenix secret (DISCORD_BOT_TOKEN / DISCORD_ALLOWED_USERS
    # / DISCORD_HOME_CHANNEL), merged into the default profile's .env via
    # services.hermes-agent.environmentFiles above.
    platforms.discord.enabled = true;
    gateway.profile_routes = [
      {
        name = "ha-voice";
        platform = "homeassistant";
        profile = "ha";
      }
    ];
    platform_toolsets.homeassistant = [ "homeassistant" ];
  };

  # ── Hermes web dashboard ─────────────────────────────────────────────
  # Web admin panel (`hermes dashboard`), exposed on hermesagent.lan:9119
  # over the tailnet only. Binding 0.0.0.0 engages the dashboard's own auth
  # gate; the `basic` (username/password) provider reads its credentials
  # from the dashboard-env agenix secret, appended to the default profile's
  # .env — same pattern as the API_SERVER_KEY / hindsight keys.
  services.hermes-dashboard = {
    enable = true;
    environmentFile = "/run/agenix/hermes.env";
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
  # Editable, low-stakes, independently revertible. Source is fetched as a
  # GitHub input at build time; notes vault is a git clone of codeberg
  # notes.git via the xaelwiki SSH deploy key. The MCP bearer token comes
  # from xaelwiki-env; the deploy key from xaelwiki-ssh.
  services.xaelwiki = {
    enable = true;
    environmentFile = "/run/agenix/xaelwiki.env";
    sshKeyFile = config.age.secrets."xaelwiki-ssh".path;
  };

  # ── Playwright MCP browser automation ──────────────────────────────────
  # Headless browser MCP server (navigate, click, fill, screenshot) on loopback.
  services.playwright-mcp.enable = true;

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
    environmentFile = "/run/agenix/hermes-nicegui.env";
    gatewayTokenFile = "/run/agenix/hermes-nicegui.env";
    darkMode = true;
  };

  # ── Hermes emergency recovery ──────────────────────────────────────
  # Periodic check (every 30 min) of hermes-agent + hermes-nicegui health.
  # If either is down, attempts restart; if that fails, launches opencode
  # to diagnose and recover, then creates a kanban post-mortem ticket.
  # This is the final oh-shit safety net — runs as root for full control.
  services.hermes-emergency-recovery = {
    enable = true;
  };

  # Loopback-only Xvfb+x11vnc display for human-in-the-loop tasks, served
  # through the vnc plugin tab. Unit exists always but is started ON DEMAND
  # by the bot (`systemctl start hermes-vnc`) — never at boot.
  services.hermes-vnc.enable = true;

  # ── Log Monitor ───────────────────────────────────────────────────────
  # Watches hermes-agent, hermes-nicegui, and podman-hindsight logs for
  # errors and opens deduplicated kanban tickets (one per service).
  services.hermes-log-monitor.enable = true;

  # Operator mandate t_89f84f69: pin model via hermesDeploy.providers so all
  # services (hermes, opencode, hindsight, nicegui, ha) use it.
  hermesDeploy.providers.local = {
    name = "local";
    provider = "vllm";
    model = "QuantTrio/Qwen3.6-35B-A3B-AWQ";
    baseUrl = "http://192.168.193.96:8000/v1";
  };
  hermesDeploy.providers.openrouter = {
    name = "openrouter";
    provider = "openrouter";
    model = "google/gemini-2.5-flash";
  };
  hermesDeploy.defaultProvider = "local";
  hermesDeploy.fallbackProviders = [ "openrouter" ];
  hermesDeploy.hindsight.port = 8888;

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

  # Tailscale ssh: pin the Nix shadow login into the tailscaled service PATH.
  # This VM still carries a Debian-era /usr (pre-NixOS-conversion residue)
  # whose /usr/bin/login is a generic-ELF binary that NixOS's stub-ld loader
  # refuses to exec. If the daemon's PATH ever searches /usr/bin, `tailscale
  # ssh` dies with "Could not start dynamically linked executable:
  # /usr/bin/login" (seen 2026-08-17: the daemon then ran with a stale
  # Debian-era environment; current sessions are fine). Pinning shadow keeps
  # the modern ssh-session path free of the broken binary and gives TTY
  # sessions a proper PAM login shell even if the tailnet later flips this
  # node to modern (V2) ssh behavior.
  systemd.services.tailscaled.path = lib.mkAfter [ pkgs.shadow ];

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
    # hermes-ha proxy (:8444): Home Assistant (hearth, on the LAN) talks to
    # the ha profile's OpenAI-compatible endpoint here, bearer-auth'd by the
    # API_SERVER_KEY. The tailnet side of :8444 is opened by the exposure
    # registry (modules/exposure.nix → interfaces.tailscale0), so this rule
    # only needs the LAN interface.
    interfaces.eth0.allowedTCPPorts = [ 8444 ];
  };

  # ── Cron ─────────────────────────────────────────────────────────────
  # cronie for the unattended weekly `nix flake update` crontab entry
  # (installed in the hermes user crontab; kanban t_70a5fd2e). User
  # crontabs live in /var/spool/cron/crontabs/.
  services.cron = {
    enable = true;
  };

  # ── System ───────────────────────────────────────────────────────────
  # Run the whole box on the user's clock: America/Halifax (ADT UTC-3 /
  # AST UTC-4). Home Assistant (config.time_zone) and the Hermes app config
  # (config.yaml timezone) already run America/Halifax; the box system clock
  # was the odd one out (UTC), which made system-clock readers (food log day
  # bucketing in food_log.py, cron exprs) disagree with the user's local day
  # by 3h (4h in AST). With the box on the user's TZ, `datetime.date.today()`
  # and cron exprs mean LOCAL wall time year-round, DST included.
  time.timeZone = "America/Halifax";

  # ── User linger ──────────────────────────────────────────────────────
  # Linger ensures a real user systemd manager (not just "manager-early")
  # and a user D-Bus session bus are running for the hermes user even when
  # no one logs in interactively. This is required for cronjobs that use
  # systemd-run --user --scope (restart-safe dispatch) — the
  # hermes-nicegui E2E monitor, shepherd-window, fitness targets, etc.
  users.users.hermes.linger = true;
  system.stateVersion = "26.05";

  # ── NixOS dynamic linker ────────────────────────────────────────────
  # programs.nix-ld: installs a custom dynamic linker (ld-linux) that knows
  # about all Nix store libraries. Without this, non-Nix processes
  # (virtualenv Python, system Python, Playwright, any pip-installed C
  # extension) cannot find libstdc++.so.6, libpython, etc. — they only see
  # /lib and /usr/lib which are empty on NixOS. Required for pip installs,
  # virtualenvs, and any compiled Python packages in user-managed environments.
  programs.nix-ld.enable = true;
  programs.nix-ld.libraries = [
    pkgs.stdenv.cc.cc.lib
  ];

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
