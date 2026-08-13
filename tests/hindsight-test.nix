# NixOS integration test for the Hindsight service wiring.
#
# Boots a VM with the hindsight + llm modules and asserts that:
#   * the podman OCI container unit is generated with the right image, network
#     and environment,
#   * the generic preferred-model options (hermesDeploy.llm) are wired through,
#   * the unit is disabled at boot (autoStart=false) so the test never pulls
#     the image.
#
# Run with:  nix build .#checks.x86_64-linux.hindsight-integration

{ nixpkgs, ... }:
let
  system = "x86_64-linux";
  inherit (nixpkgs) lib;
  pkgs = nixpkgs.legacyPackages.${system};
in
pkgs.testers.runNixOSTest {
  name = "hindsight";

  nodes.machine =
    { ... }:
    {
      imports = [
        ../modules/llm.nix
        ../modules/hindsight.nix
      ];

      services.hindsight = {
        enable = true;
        autoStart = false; # don't pull the ~2GB image in the test
        environmentFile = "/var/lib/hindsight/empty.env";
      };

      system.activationScripts.hindsight-empty-env = lib.stringAfter [ "users" ] ''
        install -d -m 0755 /var/lib/hindsight
        install -o root -g root -m 0400 /dev/null /var/lib/hindsight/empty.env
      '';

      fileSystems."/" = {
        device = "/dev/vda1";
        fsType = "ext4";
      };
      boot.loader.grub.device = "/dev/vda";

      system.stateVersion = "26.05";
    };

  testScript = ''
    import re

    start_all()
    machine.wait_for_unit("multi-user.target")

    # `script` renders as a generated store script; ExecStart only points at it.
    unit = machine.succeed("systemctl cat podman-hindsight.service")
    m = re.search(r"ExecStart=\s*(\S+)", unit)
    assert m, "ExecStart not found in unit"
    script = machine.succeed(f"cat {m.group(1).strip()}")

    assert "ghcr.io/vectorize-io/hindsight@sha256:" in script, "image not pinned by digest"
    assert "--network=host" in script, "expected host networking for firewall-gated tailnet access"
    assert "-v /var/lib/hindsight:/home/hindsight/.pg0" in script, "pg0 data volume missing"
    assert "--env-file /var/lib/hindsight/empty.env" in script, "environment file not wired"

    # Generic preferred-model options flow through to the container env.
    assert "HINDSIGHT_API_LLM_MODEL=deepseek-v4-flash" in script
    assert "HINDSIGHT_API_LLM_BASE_URL=https://opencode.ai/zen/go/v1" in script
    assert "HINDSIGHT_API_TENANT_EXTENSION" in script, "API-key auth not enabled"

    # autoStart=false -> unit must not be pulled in by multi-user.target.
    machine.succeed("test ! -e /etc/systemd/system/multi-user.target.wants/podman-hindsight.service")

    print("hindsight integration test passed")
  '';
}
