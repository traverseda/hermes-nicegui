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
  #
  # Real source code — the bot's own runtime (vendor/hermes-agent), and the
  # nicegui/xaelWiki plugins that run in-process with it — is vendored as git
  # submodules and built into the store like everything else in the system
  # lane: no live/editable checkout, no restart-to-apply. Only content
  # (skills, self-written tool scripts, MCP registrations — see
  # modules/hermes-tools.nix) gets the fast, no-rebuild content lane. See
  # README "Two change lanes".

  inputs = {
    # Keep the same channel as the build machine so eval behaves predictably.
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    # Hermes ships its own flake + NixOS module. VENDORED as a git submodule
    # (vendor/hermes-agent) rather than a pinned github: rev: this is the
    # bot's own source code, and any change to it must go through the same
    # commit -> nixos-rebuild switch -> generation pipeline as everything
    # else in the system lane (see README "Two change lanes"). `git+file:`
    # fetches the submodule's OWN git history at whatever commit is checked
    # out — uncommitted edits are invisible to the build (must be committed
    # first, inside the submodule), and flake.lock pins the exact commit, so
    # `nixos-rebuild switch` always builds a fully reproducible, immutable
    # closure. To pick up a new commit made inside the submodule:
    #   nix flake lock --update-input hermes-agent
    # Rollback is a normal generation rollback: modules/hermes-deploy.nix
    # additionally resets the outer repo (and `git submodule update`s every
    # vendor/* submodule) to the commit that produced the target generation,
    # so the on-disk checkout and the running closure never disagree.
    #
    # NB: `git+file:./relative/path` prints a Nix deprecation warning
    # ("relative path... will stop working in a future release", nix#12281).
    # It's still the correct fetcher here — `path:` was tried and rejected:
    # it resolves against the OUTER repo's own git tracking rather than
    # treating vendor/* as an independent nested repo, so it can't see a
    # submodule's own commits at all. An absolute `git+file:///...` would
    # dodge the warning but break portability (this repo lives at a
    # different absolute path on the dev machine vs. the LXC's
    # /var/lib/hermes-deploy). Revisit when nix#12281 lands a real fix.
    hermes-agent = {
      url = "git+file:./vendor/hermes-agent?shallow=false";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    # hermes-nicegui and xaelWiki: same vendoring principle as hermes-agent
    # above, just `flake = false` since they're plain source trees, not
    # flakes themselves — modules/hermes-nicegui.nix and modules/xaelwiki.nix
    # build a package from the fetched source.
    hermes-nicegui-src = {
      url = "git+file:./vendor/hermes-nicegui";
      flake = false;
    };
    xaelwiki-src = {
      url = "git+file:./vendor/xaelWiki";
      flake = false;
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
      hermes-nicegui-src,
      xaelwiki-src,
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
        ./modules/hermes-secrets.nix
        ./modules/hermes-service.nix
        ./modules/hermes-skills.nix
        ./modules/hermes-deploy.nix
        ./modules/hermes-tools.nix
        ./modules/hermes-ha.nix
        ./modules/hermes-dashboard.nix
        ./modules/opencode.nix
        ./modules/config.nix
        ./modules/hindsight.nix
        ./modules/exposure.nix
        ./modules/cloudflare-tunnel.nix
        ./modules/xaelwiki.nix
        ./modules/hermes-nicegui.nix
        ./modules/hermes-vnc.nix
      ];

      hermes = lib.nixosSystem {
        inherit system;
        modules = commonModules ++ [ ./hosts/hermes/configuration.nix ];
        specialArgs = { inherit hermes-nicegui-src xaelwiki-src; };
      };

      # A VM build of the same shared modules (no proxmox-lxc specifics),
      # used for fast local iteration on the hermes service & deploy machinery.
      hermesVm = lib.nixosSystem {
        inherit system;
        modules = commonModules ++ [
          ./tests/vm-configuration.nix
        ];
        specialArgs = { inherit hermes-nicegui-src xaelwiki-src; };
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
          inherit nixpkgs hermes-agent;
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
          inherit nixpkgs hermes-agent xaelwiki-src;
        };
        tools-config-check = import ./tests/tools-config-check.nix {
          inherit nixpkgs hermes-agent;
        };
        nicegui-config-check = import ./tests/nicegui-config-check.nix {
          inherit nixpkgs hermes-agent hermes-nicegui-src;
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
