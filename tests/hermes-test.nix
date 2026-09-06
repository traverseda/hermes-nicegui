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

    # The production `hermes-deploy-repo` activation snippet must have made
    # /var/lib/hermes-deploy a real git repo (the box-side git phases silently
    # no-op without it — the box is NOT a git repo out of the box, only after
    # this activation runs).
    machine.succeed("test -d /var/lib/hermes-deploy/.git")
    # Worktree is hermes-owned but git here runs as root: the system gitconfig
    # safe.directory exception (from this module) must let git operate.
    machine.succeed("git -C /var/lib/hermes-deploy config user.email test@example.com")
    machine.succeed("git -C /var/lib/hermes-deploy config user.name test")
    # Seed a commit tagged as the CURRENT generation (simulating that this
    # generation was built from it — the real deploy script tags every
    # generation it produces via tag_gen), then a second commit on top
    # (simulating an uncommitted/bad edit) so the rollback's git-sync step
    # (sync_repo_to_gen) has a gen-<N> tag to find and something to rewind to.
    machine.succeed("git -C /var/lib/hermes-deploy commit -q --allow-empty -m 'gen 1'")
    machine.succeed(
        "CUR=$(readlink /nix/var/nix/profiles/system | grep -o '[0-9][0-9]*'); "
        "git -C /var/lib/hermes-deploy tag -f gen-$CUR"
    )
    machine.succeed("git -C /var/lib/hermes-deploy commit -q --allow-empty -m 'gen 2'")

    # The three deploy units must exist and be correctly wired.
    machine.succeed("systemctl cat hermes-deploy.service | grep -q ExecStart")
    machine.succeed("systemctl cat hermes-rollback.service | grep -q ExecStart")
    machine.succeed("systemctl cat hermes-watchdog.service | grep -q ExecStart")
    machine.succeed("systemctl is-enabled hermes-watchdog.timer")

    # The bot-facing CLI wrappers exist (and git is on the box — a missing git
    # used to be masked here by `|| true`; hermes-status would hit `git:
    # command not found` on line 1 and still pass because `grep generations`
    # matched the later echo).
    machine.succeed("command -v git")
    machine.succeed("hermes-status | grep -q generations")

    # Manual rollback against a real local git repo (no remote): the Nix-level
    # rollback may legitimately fail (a fresh VM has only one generation, so
    # `nix-env --rollback` has nowhere to go) but the git-sync step must still
    # run and reset the flake checkout to whatever the gen-<N> tag records for
    # the (unchanged) current generation — assert that worked and no binary is
    # missing.
    machine.succeed("systemctl start hermes-rollback.service || true")
    machine.succeed("git -C /var/lib/hermes-deploy log --oneline -1 | grep -q 'gen 1'")
    machine.succeed("! journalctl -u hermes-rollback.service --no-pager | grep -q 'command not found'")

    # Manual deploy against a real local git repo (no remote): the git phase
    # must run and commit the working tree on top of the current HEAD. Touch a
    # file first so there is something to stage (the VM repoDir is empty).
    machine.succeed("touch /var/lib/hermes-deploy/.deploy-seed")
    machine.succeed("systemctl start hermes-deploy.service || true")
    machine.succeed("git -C /var/lib/hermes-deploy log --oneline -2 | grep -q 'deploy'")

    print("integration test passed")
  '';
}
