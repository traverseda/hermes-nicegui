# Recovery System Hardening — Red Team Fixes

## Goal

Prevent hermes from accidentally killing itself through the OnFailure crash-recovery chain, and ensure every failure mode is visible and stoppable.

## Issues Found (Red Team Audit)

1. [P0] OnFailure chain unconditionally fires — no `last-known-good` guard
2. [P0] Nix GC (`--delete-older-than 30d`) can prune all rollback targets on a low-generation system
3. [P1] Empty `contentRecoveryScript` exits 0 when `hermes-tools` is disabled, suppressing OnFailure escalation
4. [P2] `nicegui` crashes consume Nix generations (same OnFailure chain as agent)
5. [P2] `sync_repo_to_gen` logs a WARNING but continues if gen tag is missing — drifts system into inconsistent state
6. [P3] Bot self-deploy can commit mid-flight content changes from `hermes-content-commit` timer
7. [P3] `last-known-good` missing means OnFailure never rolls back (watchdog path checks, OnFailure path doesn't)

---

## Fix 1: OnFailure Chain Must Respect `last-known-good` (P0)

**File:** `modules/hermes-deploy.nix`
**Current:** Lines 478-505 — `hermes-content-recovery-run` and `hermes-rollback-run` fire unconditionally.
**Problem:** If only 1 generation exists, or `last-known-good` doesn't exist, there's nothing to rollback to — but the chain still fires. Content recovery dies (no `content-good` tag), rollback dies (no previous generation), agent stays crashed forever.

**Change:**
- Create a guard script (`hermes-recovery-guard.sh`) that checks at least one valid rollback target exists before allowing the chain to fire.
- The guard checks:
  1. `nix-env --rollback --dry-run` or `nix-env -p ... --list-generations` shows ≥ 2 generations
  2. OR at least one `gen-*` tag exists in the repo
  3. AND `last-known-good` file exists (at least one healthy baseline)
- If guard fails → unit exits 1 with clear error message to syslog/dmesg — human must intervene.
- The guard script replaces the current `ExecStart` on `hermes-content-recovery-run`.

This prevents the infinite crash-recover-crash loop where both recovery and rollback exhaust themselves.

---

## Fix 2: Nix GC Configuration (P0)

**File:** `modules/hermes-deploy.nix`
**Current:** Line ~509 — `nix.gc.options = "--delete-older-than 30d"`
**Problem:** On a minimal gen system (3-5 generations), 30-day GC can delete the only rollback target if both recent generations go bad. The `--delete-older-than` flag is time-based; the system may be up ≤30 days with only 2-3 generations.

**Change:**
- Replace time-based GC with size-based limits:
  ```nix
  nix.gc = {
    automatic = true;
    options = "--max-use 8G --delete-older-than 7d";
    # Or simpler, just prune old gens conservatively:
    # dates = "weekly";
    # options = "--delete-older-than 30d";
    # BUT add a separate invariant that keeps at least 5 gens.
  };
  ```
- Add a systemd override or a `nix.gc.extraOptions` that keeps ≥ 5 generations, since one generation per day of work is the expected cadence and rollback steps back one at a time.

Actually, the simplest fix is to change the GC to `--delete-older-than 30d` AND add a separate Nix setting that caps how many old stores get deleted:

```nix
nix.gc = {
  automatic = true;
  dates = "weekly";
  options = "--max-jobs 1 --delete-older-than 30d";
};
# Keep at least 5 generations — one per day for a week's worth of self-edits
nix.settings.referenced-gc-roots-patterns = [ ];  # no-op, need separate approach
```

The cleanest approach: use `nix.store.max-generations` via a gc hook or just switch to:
```nix
nix.gc = {
  automatic = true;
  dates = "weekly";
  options = "--delete-older-than 14d";  # shrink window for low-RAM boxes
};
```

14 days is more conservative (RAM is constrained to 3G MemoryMax on builds) and ensures at least 2 weeks of rollback history. The real protection is **not letting GC fire during a bad deploy** — add `nix.gc.extraOptions` with `nix.gc.extraOptions = "--options extra-access-roots /nix/var/nix/gcroots/hermes-gcguard"` so we can pin roots for recent gens.

**Final plan:** Set `nix.gc.dates = "weekly"` (not daily — daily GC on first deployment has burned gens before based on the comment pattern). Add a gc root pinning mechanism: activation script creates hardlinks in `/nix/var/nix/gcroots/hermes-keep/` for the 5 most recent generations so they survive GC.

**Files:** `modules/hermes-deploy.nix` (gc config) + activation script in `hermes-deploy-repo` section.

---

## Fix 3: Empty ContentRecoveryScript (P1)

**File:** `modules/hermes-deploy.nix`
**Current:** Lines 80-82 — `contentRecoveryScript = lib.optionalString (cfg.contentRecovery != "") (pkgs.writeShellScript ...)`.
**Problem:** When `hermes-tools` is disabled, `cfg.contentRecovery` defaults to `""` (empty). `lib.optionalString (cond) value` returns empty string when `cond` is false. Then `pkgs.writeShellScript "name" ""` creates a script with no content — which Nix typically renders as just `true`, exiting 0. This means the OnFailure chain sees exit 0 and never fires the rollback-run escalation.

**Change:**
- When `hermes-tools` is NOT enabled, remove the `hermes-content-recovery-run` service entirely (don't create it).
- The `hermes-agent`/`hermes-nicegui` OnFailure should point directly to `hermes-rollback-run.service` when no content lane exists.
- This is a configuration-time change: use `lib.mkIf cfg.contentRecovery != ""` to conditionally wire the two-stage chain vs a direct one-stage chain.

**Files:** `modules/hermes-deploy.nix` lines 478-505 (systemd service definitions).

---

## Fix 4: Separate Nicegui Crash from Agent Crash (P2)

**File:** `modules/hermes-service.nix`
**Current:** Both `hermes-agent` and `hermes-nicegui` have `OnFailure = "hermes-content-recovery-run.service"`.
**Problem:** Nicegui is a thin UI — if it crashes, it's almost never a Nix config issue. Rolling back a Nix generation for a nicegui-only crash wastes generations and may roll back a perfectly healthy agent.

**Change:**
- Keep `hermes-nicegui` OnFailure → a lightweight `hermes-nicegui-restart.service` that just does `systemctl restart hermes-nicegui`.
- Keep `hermes-agent` OnFailure → `hermes-content-recovery-run.service` (full chain with rollback).
- If nicegui crash count exceeds a threshold (e.g., 5 restarts in 30min), escalate to the full content-recovery chain. But this is a later improvement; the immediate fix is just separating the chains.

**Files:** `modules/hermes-service.nix` line ~204, plus add a systemd restart unit in `modules/hermes-deploy.nix` or `modules/hermes-nicegui.nix`.

---

## Fix 5: `sync_repo_to_gen` Fatal on Missing Tag (P2)

**File:** `modules/hermes-deploy.nix`
**Current:** Lines 132-141 — `sync_repo_to_gen` logs WARNING and continues on missing tag.
**Problem:** Nix rolls back to Gen-N-1 but git stays at HEAD (bad commit). Next deploy repackages HEAD → redeploys same broken code. System is in a permanently inconsistent state.

**Change:**
- If tag is missing, exit 1 and write an alert to syslog.
- For the deploy script: this stops the health check from writing `last-known-good`, leaving the system in a known-failed state (human must fix).
- For the OnFailure chain: this triggers syslog alert so DMI/operator can see it.
- Do NOT silently continue. The WARNING log is insufficient — the system needs to visibly fail.

**Files:** `modules/hermes-deploy.nix` lines 132-141 (`sync_repo_to_gen` function).

---

## Fix 6: Prevent Mid-Flight Content Commits During Deploy (P3)

**File:** `modules/hermes-deploy.nix` + `modules/hermes-tools.nix`
**Current:** `hermes-content-commit` timer fires every 15min. `hermes-deploy` script does `git add -A && git commit`.
**Problem:** If the timer and deploy script run concurrently, `git add -A` can pick up a partially-written file from the auto-commit's `cp config.yaml` step, resulting in corrupted commits.

**Change:**
- Add a file lock (e.g., `/var/lib/hermes-deploy/.deploy.lock`) that both the deploy script and the auto-commit service must acquire before touching git.
- Use `flock -n /var/lib/hermes-deploy/.deploy.lock` — if locked, auto-commit skips with a log message (non-fatal). If deploy gets locked (unlikely, timer runs as hermes user, deploy runs as root), deploy waits with timeout.
- Apply the same locking pattern in `hermes-tools.nix` for the `hermes-content-commit` service.

**Files:** `modules/hermes-deploy.nix` (deployScript git commit step) + `modules/hermes-tools.nix` (hermes-content-commit script).

---

## Fix 7: OnFailure Always Checks for Rollback Targets (P3)

**File:** `modules/hermes-deploy.nix`
**Current:** Watchdog (in dead `hermes-deploy.nix` root) checks `last-known-good` before rolling back. OnFailure chain (active `modules/hermes-deploy.nix`) does not.
**Problem:** `last-known-good` is the only record of "this generation was once healthy." If it's missing/corrupted, the watchdog bails out (safe). The OnFailure chain fires anyway (unsafe).

**Change:**
- This is already addressed by Fix 1 (guard script). The guard script explicitly checks for `last-known-good` existence and validity before allowing recovery to proceed.

---

## Implementation Order

1. **Fix 5** (sync_repo fatal) — smallest change, highest impact. 10 lines, zero risk.
2. **Fix 3** (empty contentRecovery) — simple mkIf wiring, 5 lines. Zero risk.
3. **Fix 4** (nicegui separate chain) — add one restart unit + adjust OnFailure wiring, 15 lines. Low risk.
4. **Fix 6** (flock locking) — needs testing for false lock conflicts, moderate risk.
5. **Fix 2** (Nix GC + gen pinning) — needs testing on a live system, moderate risk (wrong pinning = disk space exhaustion).
6. **Fix 1** (guard script) — most complex, depends on understanding all edge cases. Implement after fixes 2-5.
