---
name: ticketing
description: File a kanban ticket (and work one) whenever a request needs something done outside your approved capability; never guess or silently drop work.
version: 1.0.0
author: Hermes Agent (hermes-deploy)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [ticketing, kanban, escalation, work-queue, multi-agent, escalation]
    related_skills: [hermes-agent]
---

# Ticketing

This deployment runs a Hermes kanban board as its ticket system: a durable
SQLite queue shared by every profile, driven by `kanban_*` tools. **Anytime a
request needs something done that you cannot fully complete yourself, open a
ticket instead of refusing, guessing, or silently dropping it.** Tickets are
cheap; silent misses are not.

You have kanban tools because your profile lists `kanban` in its toolsets.
The dispatcher (embedded in the gateway) picks up ready tickets and spawns the
assigned profile as a worker.

## When to file a ticket

File when a request (or a task you're stuck on) needs:

- a capability, integration, entity, credential, or tool you do not hold
- a change to the NixOS deployment / flake (ha profile: always — never touch it)
- a human decision, a physical action, or an external system
- more context than this session can resolve (ambiguous but consequential)
- the operator's attention even if no action is needed yet

Do NOT file for things you can answer or do directly with approved tools, or
for ordinary conversation.

## How to file (any profile)

Call `kanban_create` with:

| Param | Value |
|---|---|
| `title` | one line: what needs to be done |
| `assignee` | `default` (the deploy profile) unless the work belongs to another named profile |
| `body` | the request verbatim, what you already tried, what is missing, acceptance criteria ("done means …"), and any link/transcript reference |
| `skills` | `ticketing` when the worker should re-read this skill |
| `priority` | `high` if a currently-broken function is involved |

Then `kanban_comment` (or `kanban_attach`) with the originating session id /
conversation path so whoever picks it up has full context. The creator's
session is woken automatically (`auto_subscribe_on_create`) when the task
reaches a terminal event, so the requester learns the outcome.

## Working a ticket (dispatcher-spawned worker)

1. `kanban_show` — read the title, body, parent handoffs, prior attempts, and
   the full comment thread before doing anything.
2. `cd $HERMES_KANBAN_WORKSPACE` and do the work there.
3. `kanban_heartbeat(note=...)` for anything longer than a few minutes —
   stale runs get reclaimed.
4. Finish with `kanban_complete(summary=..., metadata={...})`, or
   `kanban_block(kind="needs_input"|"capability"|..., reason=...)` if stuck.
   Never guess when a block is the honest answer.

### Metadata handoff shape

```json
{
  "changed_files": ["path/to/file"],
  "verification": ["how you proved it works"],
  "dependencies": ["parent task id or external reference"],
  "blocked_reason": null,
  "residual_risk": ["what is deliberately left open / needs human review"]
}
```

Keep secrets and raw logs out of `summary`/`metadata` — pointers and summaries
only.

## Deploy-profile specifics

When the `default` profile works a ticket on this box:

- changes ship ONLY through the git-driven deploy flow at `/var/lib/hermes-deploy`:
  edit the flake, commit, push, then `hermes-deploy` (systemd) switches a
  generation, health-checks via `hermes doctor`, and auto-rolls-back on failure.
- if the ticket needs secrets, physical action, or a decision the requester must
  make, block it (`needs_input`) rather than guessing.

## Operator triage (human)

Board: `hermes kanban list` / `hermes kanban tail` / `hermes kanban show <id>`,
or `hermes dashboard` → Kanban tab. Assign with `hermes kanban assign <id> --assignee default` (or drag in the dashboard). Complete with `hermes kanban complete <id> --result "…"`.
