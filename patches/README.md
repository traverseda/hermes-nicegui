# Patching Hermes

Hermes ships as a pinned flake input (`hermes-agent` in `flake.nix`), and its
Nix package builds from *its own* source tree (`nix/lib.nix` hardcodes
`repoRoot = ./..`) with no `src`/`patches` override hook. There is no clean way
to inject patches into a pinned `github:` input directly.

So patches are shipped through a **fork**:

- The patch **content** lives here, in `patches/*.patch` — versioned in this
  repo, so reverting a patch is a normal `git revert` + redeploy.
- `scripts/patch-hermes.sh` applies those `.patch` files **onto the pinned
  upstream rev** (`patches/upstream.lock`), commits them to the fork's
  `patched` branch, points `flake.nix` at the fork, and re-locks
  (`nix flake lock --update-input hermes-agent`).
- The flake pins the fork's resulting **rev** in `flake.lock`, so the built
  Hermes is reproducible, and rollback is just reverting the flake.nix/flake.lock
  change and redeploying.

The fork exists only to bridge patches into the immutable build. All reviewable
history and rollback lives in this repo.

## One-time setup

1. Create a fork of `https://github.com/NousResearch/hermes-agent` once
   (GitHub UI, or any git host you can push to).
2. Make sure your machine can push to it.

## Workflow: add / change a patch

1. Produce a unified diff against the pinned rev
   (`patches/upstream.lock`), usually from a checkout:

   ```sh
   git clone https://github.com/NousResearch/hermes-agent
   git -C hermes-agent checkout "$(cat patches/upstream.lock)"
   # edit hermes-agent/…, then:
   git -C hermes-agent diff > patches/00-my-fix.patch
   ```

2. Apply + verify:

   ```sh
   HERMES_FORK=github:you/hermes-agent \
   HERMES_FORK_REMOTE=git@github.com:you/hermes-agent.git \
     scripts/patch-hermes.sh --dry-run
   ```

   `--dry-run` applies the patchset to a temp clone and prints the resulting
   commit without pushing or touching the flake. Drop `--dry-run` to push the
   `patched` branch, rewrite `flake.nix`, and update `flake.lock`.

3. Commit and deploy:

   ```sh
   git add patches/ flake.nix flake.lock && git commit -m "hermes: apply local patch <name>"
   scripts/deploy.sh
   ```

   (Add `--build` to `patch-hermes.sh` if you want to verify the toplevel
   builds before committing.)

## Workflow: update upstream

New upstream revisions land the same way — rebase the patchset:

```sh
scripts/patch-hermes.sh --update-upstream <new-hermes-rev>
```

This records the new rev in `patches/upstream.lock`, rebuilds the fork from it
(patches must re-apply), and re-locks. If a patch no longer applies, `--dry-run`
shows exactly which one.

## Workflow: drop a patch / roll back

- **Drop a patch:** delete `patches/<name>.patch`, re-run
  `patch-hermes.sh` (a new `patched` commit), commit, deploy.
- **Roll back a deployed patch:** `git revert` the commit that bumped
  `flake.nix`/`flake.lock`, then `scripts/deploy.sh`. That is a normal Nix
  generation switch — the watchdog and Proxmox snapshot safety nets apply.

## Patch format

- One or more `patches/*.patch` (or `*.diff`) files, applied with `git apply`
  in lexical order.
- `git diff` and `git format-patch` output both work.
- Name files `00-…`, `10-…` to control order.
- Prefer minimal, single-purpose patches; they rebase cleanly onto upstream.

## What the bot can and can't do

Hermes itself (native, hardened mode) cannot push to the fork — no credentials
on the LXC. Patch authoring and `patch-hermes.sh` run on a dev machine (or any
machine that can push to the fork). On the LXC the bot only ever sees the
resulting pinned flake, exactly like any other flake change.

## Why not X?

- **Patches in the fork only** — content wouldn't be in this repo, breaking
  `git revert`-style rollback and review.
- **`services.hermes-agent.package.override`** — the upstream derivation takes
  no `src`/`patches` argument; its source is hardcoded relative to its own tree.
- **Plugins / `extraPythonPackages`** — fine for additive features, but can't
  fix or extend core `hermes_agent` code. Those still work and are often the
  right first choice; see `references/contributor-guide.md` ("custom/local-only
  tools → plugin").
