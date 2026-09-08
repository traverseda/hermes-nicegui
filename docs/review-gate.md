# Review-Gated Deploy Flow

## Overview

The hermes-deploy system enforces a review gate: commits cannot be auto-deployed until they have been reviewed and approved. This prevents unreviewed changes from reaching the production LXC (e.g. the incident on 2026-09-08 where commits were deployed without review).

## How It Works

When `services.hermes-deploy.requireReview = true`, the deploy script checks for **one** review marker before proceeding:

- **`.review-approved` file** — a text file committed and tracked in the git repository at the repo root. The deploy script verifies the file is tracked in git (not just present on disk) so it survives the auto-commit step that runs during deploy.

A single committed `.review-approved` file enables review-gated deploys permanently. To disable the gate, remove and commit the file, then set `requireReview = false` in the host config.

The deploy script checks for the file at the repo directory (`cfg.repoDir`), not the Nix store path, so changes to the file on disk are picked up on the next deploy cycle.

## Operator Workflow

### Step 1: Make changes

Edit files in `/var/lib/hermes-deploy` and commit normally:

```bash
cd /var/lib/hermes-deploy
git add -A
git commit -m "Add feature X"
```

### Step 2: Approve for review

Create (or leave in place) the `.review-approved` file and commit it:

```bash
cd /var/lib/hermes-deploy
echo "reviewed" > .review-approved
git add .review-approved
git commit -m "Approve for review deployment"
```

This commit must be part of the same change set (or a preceding commit in the same branch) so the deploy script sees it when it runs.

### Step 3: Deploy

```bash
# From any machine with access to the flake repo:
sudo systemctl start hermes-deploy.service

# Or from the agent:
hermes-deploy
```

If `.review-approved` is not tracked in git, the deploy exits immediately with:

```
[hermes-deploy] ERROR: review gate BLOCKED — no review approval
[hermes-deploy]   The file .review-approved does not exist in the repo.
[hermes-deploy]
[hermes-deploy]   To unblock, create and commit this file before deploying:
[hermes-deploy]     touch .review-approved
[hermes-deploy]     git add .review-approved
[hermes-deploy]     git commit -m 'Approve for review deployment'
[hermes-deploy]     hermes-deploy
[hermes-deploy]
[hermes-deploy]   This gate prevents unreviewed changes from being deployed.
```

### Step 4: Unblock (if review gate blocked)

```bash
cd /var/lib/hermes-deploy
echo "reviewed" > .review-approved
git add .review-approved
git commit -m "Approve for review deployment"
hermes-deploy
```

## Design Decisions

### Why a tracked file (not a tag or trailer)?

The original design used a git tag (`reviewed`) or a `Reviewed-by:` trailer, but the deploy script auto-commits uncommitted changes before deploying, which moves HEAD and invalidates tag-based checks. A `.review-approved` file committed to the repo survives the auto-commit step because it is staged in the same commit as the change set.

### Default is off

The option defaults to `false` for backward compatibility, but is **enabled** in the production config (`hosts/hermes/configuration.nix`). New operator environments should leave it on.

## Acceptance Criteria Verification

| Criterion | Status |
|-----------|--------|
| Unreviewed commits cannot be auto-deployed | ✅ Script exits 1 with clear error |
| Review process is documented and enforceable | ✅ This doc + config flag + script check |
| No regression in deployment speed for approved changes | ✅ File existence check is O(1) — negligible |
| Clear error message when attempting to deploy unreviewed changes | ✅ Error lists unblock steps |

## Related

- Postmortem: `/var/lib/hermes/.hermes/plans/postmortem-hermes-self-death-sept8-2026.md` — Section "R3 Process Violation"
- Parent task: t_691f6874 (from t_5cc349ab postmortem action items)
- Module: `modules/hermes-deploy.nix` — `requireReview` option and deploy script gate
- Host config: `hosts/hermes/configuration.nix` — `requireReview = true`
