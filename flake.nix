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
    #
    # To carry LOCAL source patches (see patches/README.md), this input points
    # at a FORK whose `patched` branch is rebuilt from patches/upstream.lock +
    # patches/*.patch by scripts/patch-hermes.sh. That script rewrites this url
    # and re-locks; the default below (upstream, no patches) is the safe
    # fallback. Rollback = git revert of the url + flake.lock, then redeploy.
    hermes-agent = {
      # 802a60a fixes hermes-dashboard crash: adds "registration_lifecycle" to
      # pyproject.toml [project] so the root module ships in the venv wheel.
      url = "github:NousResearch/hermes-agent/802a60a1502da137c4084d0e383dcab95735ccf2";
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

      # Package overrides applied to every config (host + test VM). Keep
      # these minimal and commented — each is a real upstream bug we had to
      # work around, not a preference.
      packageOverlays = [
        # inline-snapshot (a checkInput of fastapi, pulled in by the
        # hermes-nicegui python env) ships 3 tests that break under the pinned
        # pytest (docs/example assertions that churn between pytest releases).
        # It is a test-only helper; skipping its self-check is safe.
        # NOTE: must override the python312 interpreter's packageOverrides —
        # overriding the top-level python312Packages attr does NOT reach
        # `python312.withPackages`, which uses the interpreter's internal set.
        (final: prev: {
          python312 = prev.python312.override {
            packageOverrides = _pyfinal: pyprev: {
              inline-snapshot = pyprev.inline-snapshot.overridePythonAttrs (old: {
                doCheck = false;
              });
            };
          };
        })
      ];

      commonModules = [
        { nixpkgs.overlays = packageOverlays; }
        hermes-agent.nixosModules.default
        agenix.nixosModules.age
        ./modules/hermes-service.nix
        ./modules/hermes-skills.nix
        ./modules/hermes-deploy.nix
        ./modules/hermes-tools.nix
        ./modules/hermes-ha.nix
        ./modules/hermes-dashboard.nix
        ./modules/llm.nix
        ./modules/hindsight.nix
        ./modules/exposure.nix
        ./modules/cloudflare-tunnel.nix
        ./modules/xaelwiki.nix
        ./modules/hermes-nicegui.nix
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

      # `nix flake check` validates everything. The *-config-check targets are
      # fast eval-time assertions on the generated units (no VM boot). The
      # hermes VM integration test (runtime behaviour) remains as the slow,
      # opt-in gate.
      checks.${system} = {
        inherit (self.packages.${system}) toplevel tarball;
        hermes-vm-build = self.packages.${system}.hermes-vm;
        hermes-config-check = import ./tests/hermes-config-check.nix {
          inherit nixpkgs hermes-agent;
        };
        hermes-integration = import ./tests/hermes-test.nix {
          inherit nixpkgs hermes-agent;
        };
        hindsight-config-check = import ./tests/hindsight-config-check.nix {
          inherit nixpkgs;
        };
        dashboard-config-check = import ./tests/dashboard-config-check.nix {
          inherit nixpkgs hermes-agent;
        };
        exposure-config-check = import ./tests/exposure-config-check.nix {
          inherit nixpkgs;
        };
        cloudflare-tunnel-config-check = import ./tests/cloudflare-tunnel-config-check.nix {
          inherit nixpkgs;
        };
        xaelwiki-config-check = import ./tests/xaelwiki-config-check.nix {
          inherit nixpkgs hermes-agent;
        };
        tools-config-check = import ./tests/tools-config-check.nix {
          inherit nixpkgs hermes-agent;
        };
        nicegui-config-check = import ./tests/nicegui-config-check.nix {
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
