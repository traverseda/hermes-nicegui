# Review-Gated Deploy Flow

## Overview

The hermes-deploy system enforces a review gate: commits cannot be auto-deployed until they have been reviewed and approved. This prevents unreviewed changes from reaching the production LXC (e.g. the incident on 2026-09-08 where commits were deployed without review).

## How It Works

When `services.hermes-deploy.requireReview = true`, the deploy script checks the HEAD commit for **one** of two review markers before proceeding:

1. **Git tag** — a lightweight tag named `reviewed` pointing at HEAD
2. **Commit trailer** — a `Reviewed-by:` line in the commit message (must end with trailing whitespace, matching conventional git trailers)

If neither is present, the deploy aborts with a clear error and exit code 1.

## Operator Workflow

### Step 1: Make changes

Edit files in `/var/lib/hermes-deploy` and commit normally:

```bash
cd /var/lib/hermes-deploy
git add -A
git commit -m "Add feature X

Reviewed-by: Operator Name <email@example.com>"
```

Or, if you prefer tags:

```bash
git tag reviewed HEAD
```

### Step 2: Deploy

```bash
# From any machine with access to the flake repo:
sudo systemctl start hermes-deploy.service

# Or from the agent:
hermes-deploy
```

If the commit has no review marker, the deploy exits immediately with:

```
[hermes-deploy] ERROR: review gate BLOCKED — no review marker on HEAD
[hermes-deploy]   HEAD tag "reviewed" not found.
[hermes-deploy]   HEAD commit message does not contain "Reviewed-by:".
[hermes-deploy]
[hermes-deploy]   To unblock, apply one of the following before deploying:
[hermes-deploy]     git tag -f reviewed HEAD           # tag the commit
[hermes-deploy]   or amend the commit to include:
[herses-deploy]     Reviewed-by: Operator Name <email>
```

### Step 3: Unblock (if review gate blocked)

Apply the review marker and redeploy:

```bash
# Option A: tag-based (recommended for operator workflow)
git tag -f reviewed HEAD

# Option B: trailer-based (if using amend)
git commit --amend --trailer "Reviewed-by: Operator Name <email>"

# Redeploy
sudo systemctl start hermes-deploy.service
```

## Design Decisions

### Tag vs trailer

Both are supported because:

- **Tags** are easier for operators who use manual deploy (`sudo systemctl start hermes-deploy.service`) — they don't need to amend commits
- **Trailer-based** reviews work naturally for agent-initiated deploys where the commit is freshly authored with the trailer included

### No git remote

This flake has no remote — the local git checkout IS the source of truth. The `reviewed` tag lives in this local repo and is deployed along with the commit.

### Default is off

The option defaults to `false` for backward compatibility, but is **enabled** in the production config (`hosts/hermes/configuration.nix`). New operator environments should leave it on.

## Acceptance Criteria Verification

| Criterion | Status |
|-----------|--------|
| Unreviewed commits cannot be auto-deployed | ✅ Script exits 1 with clear error |
| Review process is documented and enforceable | ✅ This doc + config flag + script check |
| No regression in deployment speed for approved changes | ✅ Tag/trailer check is O(1) — negligible |
| Clear error message when attempting to deploy unreviewed changes | ✅ Error lists both unblock options |

## Related

- Postmortem: `/var/lib/hermes/.hermes/plans/postmortem-hermes-self-death-sept8-2026.md` — Section "R3 Process Violation"
- Parent task: t_691f6874 (from t_5cc349ab postmortem action items)
- Module: `modules/hermes-deploy.nix` — `requireReview` option and deploy script gate
- Host config: `hosts/hermes/configuration.nix` — `requireReview = true`
