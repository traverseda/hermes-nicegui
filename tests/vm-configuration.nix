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
    ./../modules/config.nix
    ./../modules/hermes-service.nix
    ./../modules/hermes-skills.nix
    ./../modules/hermes-deploy.nix
  ];

  # Test model: point at a fake provider; no real keys needed.
  hermesDeploy.providers.mock = {
    name    = "mock";
    provider= "openrouter";
    model   = "openrouter/mock-test";
    baseUrl = "https://openrouter.ai/api/v1";
  };
  hermesDeploy.defaultProvider = "mock";

  services.hermes-deploy.enable = true;

  # In the VM there is no real agenix secret, so give hermes an empty env
  # file so the activation script doesn't fail.
  services.hermes-agent.environmentFiles = [ "/var/lib/hermes/empty.env" ];
  services.hermes-agent.workingDirectory = "/var/lib/hermes/workspace";
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

  # The real box has the flake cloned into /var/lib/hermes-deploy; the VM
  # starts empty. Give the deploy/rollback machinery a real git repo (a local
  # bare origin, two commits on `main`) so the git phases are actually
  # exercised instead of dying on `git: command not found`. Runs as a systemd
  # service, not an activation script: activation runs from the initrd where
  # git is not on PATH.
  systemd.services.hermes-git-repo = {
    description = "Test fixture: bootstrap a git repo in /var/lib/hermes-deploy";
    wantedBy = [ "multi-user.target" ];
    path = with pkgs; [ git ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      if [ ! -d /var/lib/hermes-deploy/.git ]; then
        git init --bare -q /tmp/hermes-origin.git
        git clone -q /tmp/hermes-origin.git /var/lib/hermes-deploy
        git -C /var/lib/hermes-deploy config user.email test@example.com
        git -C /var/lib/hermes-deploy config user.name test
        # The deploy tracks `main`; a fresh clone of an empty repo is on
        # git's default branch (master), so switch explicitly.
        git -C /var/lib/hermes-deploy checkout -q -b main
        git -C /var/lib/hermes-deploy commit -q --allow-empty -m "gen 1"
        git -C /var/lib/hermes-deploy commit -q --allow-empty -m "gen 2"
        git -C /var/lib/hermes-deploy push -q -u origin main
        # Simulate a working tree one commit behind origin, like a real checkout.
        git -C /var/lib/hermes-deploy reset -q --hard origin/main~1
      fi
    '';
  };

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
