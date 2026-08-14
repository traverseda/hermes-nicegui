# Friend-host template for `hermes-infect`.
#
# Copy this to hosts/<friend>/configuration.nix, then:
#   1. Set the feature toggles below for THIS friend (see the skill's
#      "Feature toggles" matrix — this is the "turn features on and off" step).
#   2. Point `disko.devices.disk.main.device` at the target's real disk
#      (`lsblk` on the machine).
#   3. Wire the friend's own agenix secrets (secrets/<friend>-*.age),
#      encrypted to the operator's pubkey + the friend's fresh host key.
#   4. Register it in flake.nix and run nixos-anywhere.
#
# This is a STARTING POINT, not a fleet: the friend owns the fork afterward.

{
  config,
  pkgs,
  lib,
  ...
}:
{
  imports = [
    # Shared stack modules come in via `commonModules` in flake.nix — this
    # file only carries the friend's per-host decisions. nixos-anywhere
    # requires the `disko` NixOS module to be imported too (add
    # `disko.nixosModules.disko` to the host's module list in flake.nix).
  ];

  # ── Boot (nixos-anywhere installs a real bootloader, not netboot) ───
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = true;

  # ── Feature toggles ─────────────────────────────────────────────────
  # Each service is gated by an `enable` flag. Flip them to shape what ships
  # in this friend's fork. Defaults are the "friend" posture (BYOK,
  # independent, no operator infra).
  services.hermes-agent.enable = true; # core agent — keep on
  services.hermes-deploy.enable = true; # self-manage from git — keep on

  services.hindsight.enable = true; # memory (friend's own keys)
  services.hermes-dashboard.enable = true; # web admin (friend's own creds)
  services.tailscale.enable = true; # friend's OWN tailnet

  # Operator infra — leave OFF unless the friend explicitly wants it.
  services.hermes-ha.enable = false; # Home Assistant
  services.xaelwiki.enable = false; # notes MCP (points at operator's codeberg)
  services.cloudflare-tunnel.enable = false; # public exposure

  # ── Model preference (ships in code; only the key is a secret) ──────
  # hermesDeploy.llm defaults to opencode-go / deepseek-v4-flash — leave as-is.

  # ── Disko disk layout (nixos-anywhere partitioning) ─────────────────
  # CHANGE device to the target's real disk.
  disko.devices.disk.main = {
    type = "disk";
    device = "/dev/sda";
    content = {
      type = "gpt";
      partitions = {
        ESP = {
          size = "512M";
          type = "EF00";
          content = {
            type = "filesystem";
            format = "vfat";
            mountpoint = "/boot";
            mountOptions = [ "umask=0077" ];
          };
        };
        root = {
          size = "100%";
          content = {
            type = "filesystem";
            format = "ext4";
            mountpoint = "/";
          };
        };
      };
    };
  };

  # ── BYOK secrets (friend's own, never the operator's) ───────────────
  # Create these with `age -e -r "$(cat ~/.ssh/id_ed25519.pub)" -r "<friend host pubkey>"`
  # and point the service environmentFiles at them (mirror hosts/hermes/configuration.nix).
  # age.secrets."hermes-env" = {
  #   file = ../../secrets/<friend>-hermes-env.age;
  #   owner = "hermes";
  #   group = "hermes";
  #   mode = "0440";
  # };
  # services.hermes-agent.environmentFiles = [ config.age.secrets."hermes-env".path ];

  # ── SSH (operator helps set up, then hands over) ─────────────────────
  services.openssh = {
    enable = true;
    settings = {
      PermitRootLogin = "prohibit-password";
      PasswordAuthentication = false;
    };
  };
  users.users.root.openssh.authorizedKeys.keys = [
    # Operator's pubkey during setup; the friend's keys after handoff.
    # "ssh-ed25519 AAAA..."
  ];

  # ── Firewall ────────────────────────────────────────────────────────
  # Tailnet ports are governed by the exposure registry (modules/exposure.nix).
  # Keep the ssh port open; if the friend runs no tailscale, open what they need.
  networking.firewall = {
    enable = true;
    allowedTCPPorts = [ 22 ];
  };

  # ── System ──────────────────────────────────────────────────────────
  system.stateVersion = "26.05";

  nix.settings = {
    experimental-features = [
      "nix-command"
      "flakes"
    ];
    trusted-users = [
      "root"
      "@wheel"
    ];
  };
}
