# Merge criteria

A test for whether a change to this repo — human-authored or bot-authored,
system lane or content lane — is worth merging. Meant to be applied by a
human reviewer, by `/code-review`, and by the bot itself before a
`hermes-tool commit` or a system-lane change.

## Why this exists

The project isn't "run one bot safely" — it's three things pulling together:

1. **Autonomy.** Give the agent real capability — root, self-modification,
   persistent memory, the ability to grow its own tools and reach into the
   physical world (Home Assistant, notes, comms) — rather than a sandboxed
   toy. Clipping its wings to make it "safe" defeats the point.
2. **Safety via undo, not restriction.** The enabling constraint that makes
   (1) survivable. Every path the bot can hurt itself through — content,
   system, secrets, health — is routed through something that can be
   mechanically undone. See README, "The bot is root": the safety net is
   rollback, not a permission gate.
3. **Sovereignty and propagation.** The end state isn't a fleet you operate
   forever — it's a pattern independent enough to fork onto someone else's
   machine, with their own keys and their own tailnet, and then hand off
   completely (see `hermes-infect`: "this is mitosis, not fleet management —
   we grow a new independent copy and let go"). A safety property that only
   holds because *you're* watching this instance, or because it shares
   *your* credentials, hasn't actually been solved — it's been deferred to
   you.

These pull against each other. A change that's easy precisely because it
assumes a shared secret, your tailnet, or your ongoing attention is trading
thread 3 for convenience. The hard gates below are the mechanical floor for
threads 1 and 2; the "survives a fork" judgment factor exists for thread 3.

## The core test

> **A change is good if it extends what the bot can safely do, without
> weakening the rollback or credential guarantees that let it run
> unsupervised with root.**

Two clauses, both required. A capability that can't be rolled back isn't
safe to ship. A safety mechanism that blocks the bot from doing its job
isn't a fix, it's a regression — the design explicitly rejects permission
gates as the safety net (see README, "The bot is root"); rollback is the
net. A change that only serves one clause needs the other addressed in the
same change, not deferred.

## Hard gates (all must pass)

These are conjunctive — failing any one is disqualifying regardless of how
good the rest of the change is.

1. **Reversible in the right lane.** The change lands in content lane
   (`hermes-tool`) or system lane (Nix generation), matching what it
   actually touches — in-process code and anything that can break the
   gateway is system lane, no exceptions for convenience. If undoing it
   takes more than `hermes-tool revert` / `hermes-rollback`, it doesn't pass.
2. **No secrets outside agenix.** Nothing plaintext in Nix config, in a
   commit, or in the vendored source. If it's a credential, it's an age
   secret referenced at runtime.
3. **Exposure stays registered.** Anything binding a tailnet-reachable port
   goes through `hermesDeploy.exposure.services` with a real `auth.type`.
   `auth.type = "none"` requires the explicit double opt-in
   (`auth.insecure = true` *and* `allowInsecure = true`) — not a default,
   not an oversight.
4. **Watchable.** If the change can break the gateway, the watchdog's
   health check (`hermes doctor`) has to be able to see that it's broken.
   A failure mode the watchdog can't observe can't be auto-rolled-back,
   which means it isn't actually covered by the safety net this repo relies
   on.
5. **Guardrails are proven, not asserted.** A new invariant ("this port is
   always credentialed," "this snippet always runs") ships with a fast
   eval-time test in `tests/*-config-check.nix` that fails if the invariant
   breaks. The README is explicit about why: the git-init activation
   snippet "has shipped broken before" — that's the standing argument for
   this gate, not a hypothetical.

## Judgment factors (weigh, don't block)

Not individually disqualifying, but a change that trips several of these
needs a better justification than "it works."

- **Blast radius matches justification.** Loopback → tailnet → public
  (Cloudflare) is an increasing commitment; a public-facing change should
  be earning that reach, not defaulting to it.
- **Restraint.** Does it do what's needed, or does it also refactor,
  generalize, or add a knob nothing asked for? Three similar lines beat a
  premature abstraction here as much as anywhere else.
- **Docs match reality in the same change.** README (and this file, if the
  invariant it documents changed) get updated alongside the code, not in a
  follow-up that may never land — a stale README has already caused a
  real incident here (the git-checkout snippet).
- **No-remote is preserved.** The build machine is the deliberate source
  of truth; a change that makes deploy or rollback depend on network
  reachability beyond the Nix closure copy is working against the design,
  even if it's convenient.
- **Survives a fork.** Would this still work, unmodified, on a friend's
  `hermes-infect` copy — their keys, their tailnet, no ongoing access from
  us? A change that's only safe because it's *our* box (a hardcoded
  credential, a dependency on our tailnet, a manual step only we remember
  to do) is quietly relying on operator attention as the real safety
  mechanism, which is the thing thread 2 exists to avoid.

## Automatic rejects

- A "live edit" that skips the git ledger for either lane.
- A system-lane change (`vendor/*`) that ships without the matching
  `flake.lock` bump — the submodule commit exists but nothing points at it,
  so rollback can't reconstruct what ran.
- A credentialed-sounding service wired with `auth.type = "none"` and no
  double opt-in.
- A test deleted or weakened to make a change pass, rather than the change
  fixed to satisfy the test.

## Using this

- **Reviewing a PR or diff:** walk the hard gates first — any failure is a
  request for changes, not a style note. Then weigh the judgment factors
  in the review comment.
- **`/code-review` on this repo:** treat the hard gates as correctness
  findings, judgment factors as simplification/efficiency findings.
- **The bot, before `hermes-tool commit` or proposing a system-lane
  change:** gates 1–4 are the ones self-checkable without a second
  reviewer — if any fails, fix it before committing rather than relying on
  the watchdog to catch it after the fact.
