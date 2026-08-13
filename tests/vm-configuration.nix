# VM configuration for local testing — the same shared modules as the LXC,
# minus proxmox-lxc specifics (which would fight with the QEMU VM boot).
#
# Boot it with:  nix run .#hermes-vm

{
  config,
  pkgs,
  lib,
  ...
}:

{
  imports = [
    ./../modules/hermes-service.nix
    ./../modules/hermes-skills.nix
    ./../modules/hermes-deploy.nix
  ];

  # Test model: point at a fake provider; no real keys needed.
  hermesDeploy.model = "openrouter/mock-test";

  services.hermes-deploy.enable = true;

  # In the VM there is no real agenix secret, so give hermes an empty env
  # file so the activation script doesn't fail.
  services.hermes-agent.environmentFiles = [ "/var/lib/hermes/empty.env" ];
  system.activationScripts.hermes-empty-env = lib.stringAfter [ "users" ] ''
    install -o hermes -g hermes -m 0640 /dev/null /var/lib/hermes/empty.env
  '';

  # Quick health check for local iteration: just the unit + pidfile.
  services.hermes-deploy.healthCheck = ''
    systemctl is-active --quiet hermes-agent \
      && [ -f /var/lib/hermes/.hermes/gateway.pid ] \
      && kill -0 "$(cat /var/lib/hermes/.hermes/gateway.pid)" 2>/dev/null
  '';
  services.hermes-deploy.gracePeriod = 30;

  # Tailscale needs a working /dev/net/tun and an auth key in the VM; skip
  # it there.
  services.tailscale.enable = lib.mkForce false;

  # Standard root filesystem + bootloader so this is a valid standalone
  # system (nix flake check builds system.build.toplevel for it). For actual
  # VM booting via `system.build.vm`, the qemu-vm module overrides these.
  fileSystems."/" = {
    device = "/dev/vda1";
    fsType = "ext4";
  };
  boot.loader.grub.device = "/dev/vda";

  system.stateVersion = "26.05";

  environment.systemPackages = with pkgs; [ vim ];
}
