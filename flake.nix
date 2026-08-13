{
  description = "Rollback-safe deployment of the Hermes LLM agent on a Proxmox LXC";

  # ── Principle ─────────────────────────────────────────────────────────
  # Anything the bot changes about itself must be safe and reversible:
  #   * The running system is a NixOS generation (immutable store).
  #   * Changes land via git -> nixos-rebuild switch (a new generation).
  #   * Rollback = nixos-rebuild --rollback (previous generation) or git revert.
  #   * A health-checked watchdog auto-rolls-back a bad deploy.
  #   * Secrets are encrypted in-repo (agenix) and decrypted only at
  #     activation time on the target.

  inputs = {
    # Keep the same channel as the build machine so eval behaves predictably.
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    # Hermes ships its own flake + NixOS module.
    # Pin to a specific rev: the project is Tier-2/best-effort, commits to
    # `main` can break the module. Update deliberately with:
    #   nix flake lock --update-input hermes-agent
    hermes-agent = {
      url = "github:NousResearch/hermes-agent/fa83af3f9a42790730b8966ff67e7d9fb627899f";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    # Secret management with age. Decrypts into /run/agenix on activation.
    agenix = {
      url = "github:ryantm/agenix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      hermes-agent,
      agenix,
      ...
    }:
    let
      system = "x86_64-linux";
      lib = nixpkgs.lib;
      pkgs = nixpkgs.legacyPackages.${system};

      commonModules = [
        hermes-agent.nixosModules.default
        agenix.nixosModules.age
        ./modules/hermes-service.nix
        ./modules/hermes-deploy.nix
      ];

      hermes = lib.nixosSystem {
        inherit system;
        modules = commonModules ++ [ ./hosts/hermes/configuration.nix ];
      };

      # A VM build of the same shared modules (no proxmox-lxc specifics),
      # used for fast local iteration on the hermes service & deploy machinery.
      hermesVm = lib.nixosSystem {
        inherit system;
        modules = commonModules ++ [
          ./tests/vm-configuration.nix
        ];
      };
    in
    {
      nixosConfigurations = {
        inherit hermes;
        # Alias so `nixos-rebuild switch --flake .#hermes-vm` also works.
        hermes-vm = hermesVm;
      };

      packages.${system} = {
        inherit (hermes.config.system.build) toplevel tarball;
        # Bootable local VM for testing: nix run .#hermes-vm
        hermes-vm = hermesVm.config.system.build.vm;
        default = self.packages.${system}.toplevel;
      };

      # `nix flake check` validates everything and runs the NixOS test.
      checks.${system} = {
        inherit (self.packages.${system}) toplevel tarball;
        hermes-vm-build = self.packages.${system}.hermes-vm;
        hermes-integration = import ./tests/hermes-test.nix {
          inherit nixpkgs hermes-agent;
        };
      };

      devShells.${system}.default = pkgs.mkShell {
        packages = with pkgs; [
          agenix.packages.${system}.default
          nixos-rebuild
          git
          ssh-to-age
          age
        ];
        shellHook = ''
          echo "hermes-deploy dev shell"
          echo "  nix flake check           # validate + run tests"
          echo "  nix run .#hermes-vm       # boot local test VM"
          echo "  scripts/deploy.sh         # deploy to the LXC"
        '';
      };

      # Convenience commands used on the LXC / by the bot itself.
      formatter.${system} = pkgs.nixfmt-rfc-style;
    };
}
