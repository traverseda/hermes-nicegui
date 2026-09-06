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

  # The test VM is memory-constrained by design (a few hundred MB) — the
  # production memory gate (default floor 2048MB) would refuse every deploy
  # here regardless of whether the deploy machinery itself works. Disable the
  # floor for this environment only; real hardware keeps the default.
  services.hermes-deploy.minAvailableMemMb = 0;

  # The real box has the flake copied into /var/lib/hermes-deploy; the VM
  # starts empty. The deploy/rollback git repo is bootstrapped in BOTH the VM
  # and the box by the production `hermes-deploy-repo` activation snippet
  # (modules/hermes-deploy.nix), so there is no VM-specific fixture here.
  # The integration test seeds commits on top of that empty repo to exercise
  # the git phases.

  # TEST-ONLY FIXTURE: a real box always has /nix/var/nix/profiles/system,
  # because its very first deploy IS a `nixos-rebuild switch` (which creates
  # it as a side effect) — hermes-deploy.nix's currentGeneration/tag_gen/
  # sync_repo_to_gen all assume it exists. `pkgs.testers.runNixOSTest` boots
  # this VM straight from a pre-built image (system.build.vm) and never runs
  # a switch inside itself, so the profile link is simply never created here
  # otherwise. Point it at the booted system so currentGeneration parses a
  # real "system-1-link" (harmless if a real `nix run .#hermes-vm` later
  # does an actual switch — nix-env --set just overwrites this).
  system.activationScripts.hermes-test-profile = lib.stringAfter [ "users" ] ''
    mkdir -p /nix/var/nix/profiles
    if [ ! -e /nix/var/nix/profiles/system-1-link ]; then
      ln -sfn /run/current-system /nix/var/nix/profiles/system-1-link
      ln -sfn system-1-link /nix/var/nix/profiles/system
    fi
  '';

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
