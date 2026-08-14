---
name: hermes-tooling
description: "Create new skills, tool scripts, and MCP servers through the git-backed content store (hermes-tool) — the fast path — instead of a Nix rebuild for every new tool. Know the revert story before you mutate."
version: 1.0.0
author: Hermes Agent (hermes-deploy)
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [hermes, tooling, skills, mcp, content-store, fast-path, rollback, self-management]
    related_skills: [hermes-agent, ticketing]
---

# Hermes Tooling (content-store fast path)

**The core unit of work on this deployment is creating new tools — and most of
them are NOT critical for the gateway to run.** They should not cost a Nix
rebuild, a new generation, or a full system deploy.

This deployment has **two change lanes**:

| | System lane (Nix) | Content lane (`hermes-tool`) |
|---|---|---|
| What changes | the gateway unit, packages, firewall, secrets, daemons | skills, tool scripts, MCP servers, config.yaml |
| How it ships | flake edit → `nixos-rebuild switch` → new generation | `hermes-tool` → auto-commit → git |
| Rollback | `hermes-rollback` (one generation) | `hermes-tool revert` (to `content-good`) |
| Restart cost | full gateway restart | skills: none. bin: none. MCP: gateway restart only |
| Owner | the flake repo | `${contentDir}` git repo at `/var/lib/hermes/content` |

**Use the content lane for anything that is content**: a skill, a helper
script, an MCP server. Use the system lane only when you genuinely need
something Nix owns (a new system package on PATH, a new daemon, firewall,
secrets).

## The recovery floor (read this first)

You cannot brick the system from the content lane — but a broken content
change can make *your own tooling* misbehave. Recovery is mechanical:

```sh
hermes-tool status      # what's in the store, what's last-known-good
hermes-tool revert      # restore content + config.yaml to content-good, restart gateway
```

- `hermes-tool` is a **/nix/store binary** — immutable. It works even if you
  delete every skill, corrupt config.yaml, or register a broken MCP server.
- Every mutation **auto-commits**; git is the rollback ledger for content,
  exactly as Nix generations are the ledger for the system.
- The deploy watchdog runs `hermes-tool revert` **before** any generation
  rollback — a broken content change is reverted at content granularity and
  never costs a system generation.
- Local skills shadow the Nix vendored baseline on name collision, but the
  baseline is still scanned alongside — deleting a local skill only un-shadows
  the immutable original, it never removes it.

**The one rule that keeps this safe: verify, then `hermes-tool mark-good`.**
After you add or change content and confirm it works, run `mark-good` so the
next `revert` restores a state you actually want.

## Creating a skill (no restart)

**You write nothing by hand. Delegate all code writing to opencode** — that is
the deployment's hard rule (same as the old box: "delegate ALL code writing to
opencode, never code directly"). You are the orchestrator and reviewer; opencode
is the writer. Never hand-edit code or skills when opencode is available.

The flow:

```sh
hermes-tool skill new <name> <category>   # scaffolds SKILL.md in content/skills
# Hand the scaffold to opencode to fill in — NOT you:
opencode --continue -q "write the SKILL.md for a <name> skill that <purpose>"
# review the result, then snapshot it:
hermes-tool commit "skill: <name>"
hermes-tool mark-good                      # after it works in a session
```

Skills land in `$HERMES_HOME/skills` (symlinked from the store) and are picked
up **next session — no gateway restart**.

## Creating a tool script (no restart)

```sh
# Ask opencode to write the script (in the content store's bin or your
# workspace), then install it — you do the plumbing, opencode does the code:
opencode --continue -q "write /var/lib/hermes/content/bin/<name> that <purpose>"
hermes-tool tool add /var/lib/hermes/content/bin/<name>
```

`content/bin` is appended to the gateway PATH (lowest precedence — an
agent-authored `git` can never shadow the system one). It's available **next
turn — no restart**. Call it from a skill or directly by name.

## Creating a tool script (no restart)

```sh
# write your script, then:
hermes-tool tool add /path/to/script.py   # installs into content/bin, chmod +x
```

`content/bin` is appended to the gateway PATH (lowest precedence — an
agent-authored `git` can never shadow the system one). It's available **next
turn — no restart**. Call it from a skill or directly by name.

## Adding an MCP server (gateway restart only)

```sh
hermes-tool mcp add <name> <url>          # or a command/args for stdio servers
hermes-tool mcp rm <name>
```

This runs `hermes mcp add`, snapshots config.yaml into the store, and restarts
the gateway. It does **not** require `nixos-rebuild`. Note the snapshot: if the
registration breaks the gateway, `hermes-tool revert` restores the known-good
config.yaml. If the MCP server itself is code you're writing (a custom tool),
have opencode write it first, then register it here.

Prefer this over editing config.yaml by hand — `hermes mcp add` validates, and
the store snapshot makes it revertible.

## When NOT to use the content lane

- You need a **new package on the gateway PATH** (system dependency for a tool)
  → flake `extraPackages`.
- You need a **new daemon / systemd service / firewall port / secret**
  → the system lane (and the exposure registry).
- The tool must be **guaranteed to survive a wiped content store** → it belongs
  in the Nix vendored skill baseline (`./skills/` in the flake).

## Safety rules

1. **Never hand-edit config.yaml for MCP servers.** Use `hermes-tool mcp add`
   (validated + snapshotted) or the flake. A stray indent can corrupt the file.
2. **Never write code or skills by hand.** Delegate to opencode; you scaffold,
   review, install, and verify. This is the deployment rule, not a preference —
   it keeps your code-review loop honest and your hands out of the text editor.
3. **Never `rm -rf` the content store.** If you must reset, use
   `hermes-tool revert` — the watchdog and operator both expect the git
   history to be intact. (A wiped store self-heals to empty on the next
   activation, but you lose your rollback history.)
4. **After a broken change, revert first, diagnose second.** `hermes-tool
   revert` is cheap and mechanical; guessing at a fix by hand risks a second
   broken commit.
5. **Verify then mark-good.** Unverified content is reverted by the watchdog's
   first unhealthy check; that is the designed safety, not a bug.
