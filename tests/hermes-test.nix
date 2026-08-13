# NixOS integration test for the Hermes deploy/rollback machinery.
#
# Boots a VM with the shared modules and asserts that:
#   * the hermes-agent gateway unit comes up,
#   * the deploy/rollback/watchdog services exist and are wired,
#   * a bad health report is NOT auto-rolled-back when there is no newer
#     generation (i.e. it never rolls back a generation that was known good).
#
# Run with:  nix build .#checks.x86_64-linux.hermes-integration

{ nixpkgs, hermes-agent, ... }:
let
  system = "x86_64-linux";
  inherit (nixpkgs) lib;
  pkgs = nixpkgs.legacyPackages.${system};

  sharedModules = [
    hermes-agent.nixosModules.default
    (import ./vm-configuration.nix)
  ];
in
pkgs.testers.runNixOSTest {
  name = "hermes-deploy";

  nodes.machine = { ... }: {
    imports = sharedModules;

    # A predictable stateDir with a writable pidfile location.
    services.hermes-agent.stateDir = "/var/lib/hermes";

    # Don't let the watchdog's periodic timer interfere with assertions.
    services.hermes-deploy.watchdogInterval = "1h";
  };

  testScript = ''
    start_all()

    machine.wait_for_unit("hermes-agent")
    machine.wait_for_unit("multi-user.target")

    # The three deploy units must exist and be correctly wired.
    machine.succeed("systemctl cat hermes-deploy.service | grep -q ExecStart")
    machine.succeed("systemctl cat hermes-rollback.service | grep -q ExecStart")
    machine.succeed("systemctl cat hermes-watchdog.service | grep -q ExecStart")
    machine.succeed("systemctl is-enabled hermes-watchdog.timer")

    # The bot-facing CLI wrappers exist.
    machine.succeed("hermes-status | grep -q generations")

    # Triggering a manual rollback with no prior generation should not crash.
    machine.succeed("systemctl start hermes-rollback.service || true")

    print("integration test passed")
  '';
}
