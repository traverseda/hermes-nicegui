# Wiring note: remove-builtin-memory.patch (2026-08-15)

`remove-builtin-memory.patch` removes Hermes' built-in file-backed memory
system (the `memory` tool, MEMORY.md/USER.md store, memory-review/nudge
machinery, CLI/slash/dashboard surfaces) while keeping hindsight and the
external memory-provider plugin architecture fully intact.

- Base rev: `802a60a1502da137c4084d0e383dcab95735ccf2` (see `upstream.lock`)
- Verified with `git apply --check` against a clean checkout of that rev.
- Produced from the kanban task workspace
  (`~/.hermes/kanban/workspaces/t_e009eda5/hermes-agent`, branch
  `remove-builtin-memory`).

## Why it is NOT yet wired into flake.nix

The documented patch flow (`scripts/patch-hermes.sh`) pushes to a fork of
`github:NousResearch/hermes-agent`. This box has **no GitHub push access**
(SSH key rejected, `gh` unauthenticated), so the fork flow is unavailable.

The alternative — switching the `hermes-agent` input to a local
`git+file://` checkout — was considered and rejected for now because it
would break the operator's build-machine deploy path: `scripts/deploy.sh`
runs `nixos-rebuild switch --target-host` from a *separate build machine*,
and a `git+file:///var/lib/hermes-deploy/hermes-agent-patched` input only
exists on the box, not on the build machine.

## How to wire it (operator decision)

Either:

1. **Fork flow (preferred):** push this patch to a fork's `patched` branch
   and run `HERMES_FORK=github:you/hermes-agent scripts/patch-hermes.sh`
   per `patches/README.md`. This keeps both deploy paths working.
2. **Local-input flow (box-only deploy):** create the patched repo on the
   box AND on the build machine at the same absolute path, e.g.
   `/var/lib/hermes-deploy/hermes-agent-patched`:
   ```sh
   git clone https://github.com/NousResearch/hermes-agent
   git -C hermes-agent checkout "$(cat patches/upstream.lock)"
   git -C hermes-agent apply patches/remove-builtin-memory.patch
   git -C hermes-agent add -A && git -C hermes-agent commit -m "remove built-in memory"
   ```
   then change the input in `flake.nix` to
   `url = "git+file:///var/lib/hermes-deploy/hermes-agent-patched";`
   and re-lock (`nix flake lock --update-input hermes-agent`).

Until one of these is done, the live box continues to build upstream
hermes-agent 0.20.1 (the patch is inert but versioned and rollback-safe in
this repo).

## Superseded in practice: config-level removal shipped (t_2f52532f, 2026-08-15)

Task t_2f52532f ("remove default memory in favour of hindsight") shipped the
equivalent removal WITHOUT the source patch, via declarative config in
`modules/hermes-service.nix`:

    memory.provider = "hindsight";
    memory.memory_enabled = false;
    memory.user_profile_enabled = false;
    agent.disabled_toolsets = [ "memory" ];

Effect on upstream 0.20.1 (verified on generation 36):
  * `memory_enabled`/`user_profile_enabled` false → MemoryStore never loads,
    MEMORY.md/USER.md blocks absent from every system prompt (prompt-size: 0 B).
  * `agent.disabled_toolsets: [memory]` → the `memory` tool is dropped from
    every platform's tool catalog (tools_config.py subtracts it last).
  * Background-review fork already gates its memory tool on
    `memory_enabled` (background_review.py #54937) → no memory writes from
    review either.
  * The hindsight provider is loaded independently of these flags
    (agent_init.py), so hindsight retain/recall/reflect keep working.

Residual difference vs applying the patch: the built-in memory code paths
still exist in the package (dead, never loaded) — cosmetic. The old
MEMORY.md file is left on disk (~/.hermes/memories/, inert) as a rollback
safety net; its durable facts were retained into hindsight. This patch stays
versioned here for the day the operator has GitHub push access and wants the
code removed outright.
