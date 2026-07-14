# SUPERVISOR

The Supervisor oversees the review → rework → merge cycle.

## Runtime / role binding

Supervisor is a controller/script contract for the orchestration loop, not a
normal spawned worker role. If an agent is asked to reason from this contract,
use the configured GPT-5.5 extra-high reasoning runtime; do not treat
`SUPERVISOR` as a separate launchable named-agent profile.

## Design Principle

**Autonomy with hard circuit breakers.**

Ordinary `REQUEST_CHANGES` results should stay autonomous and remain in the same issue lineage. Human involvement is reserved for true invariant violations.

---

## What the Supervisor Does

- reads structured reviewer outputs
- decides whether the issue can continue autonomously in the same lineage
- writes fresh feedback to `.issue-feedback.md`
- moves the issue to `Rework` when ordinary fixes are needed
- escalates only on hard invariants
- moves approved issues to `Merging`, then triggers auto-merge when enabled

It does **not** mutate `.task-prompt.md`.

---

## Core Invariants (Circuit Breakers)

| ID | Invariant | Rationale |
|----|-----------|-----------|
| I-1 | Max 3 review/rework iterations per issue by default | Prevents infinite repair loops |
| I-2 | Max 24 hours wall-clock per issue by default | Prevents runaway unattended execution |
| I-9 | Reviewers require a design-level change | Implementation should not guess through spec changes |
| I-JSON | Missing structured reviewer JSON | Machine reasoning must not depend on prose-only review |
| I-MERGE | Auto-merge failed after approval | Human intervention required |
| I-WORKTREE | Issue workspace/worktree missing for rework | Cannot continue safely |

Configuration lives in `state/supervisor/config.json`.

---

## Ordinary Review Failures Stay Autonomous

When reviewers return `REQUEST_CHANGES` and no invariant is violated:

1. Supervisor writes a fresh `.issue-feedback.md`
2. Issue state becomes `Rework`
3. `meta.auditsStarted` resets to `false`
4. Orchestrator redispatches the same issue lineage

This is the normal path. It should **not** wake the human.

---

## Approval Path

When reviewers return `APPROVE`:

1. Issue state becomes `Merging`
2. Supervisor records approval in `state/supervisor/<issue-id>.json`
3. Auto-merge runs if enabled
4. On merge success, issue becomes `Done`
5. On merge failure, issue becomes `Blocked` and an escalation record is created

Approval should **not** queue a human alert by itself.

---

## Malformed Handoffs

Malformed handoffs are handled inside the worker lineage.

Policy:
- the worker verifies its own claimed handoff before exiting successfully
- failed handoff verification writes repair feedback to `.issue-feedback.md`
- the same issue lineage may self-repair up to **2 automatic malformed-handoff repairs** by default
- after that, the issue becomes `Blocked`

Configured by:
- `maxHandoffRepairs` in `state/supervisor/config.json`

---

## Escalation Protocol

When an invariant is violated:

1. mark the issue `Blocked`
2. write `state/supervisor/escalations/<issue-id>.json`
3. queue a human alert
4. wait for one of:
   - `OVERRIDE`
   - `RETRY`
   - `ABORT`
   - `ACCEPT_RISK`

Human overrides are logged to `${OPENCLAW_HOME}/overrides.log`.

---

## Override Behavior

- `OVERRIDE` → return the issue to `Rework`
- `RETRY` → write human guidance to `.issue-feedback.md`, return to `Rework`
- `ABORT` → block the issue with an abort reason
- `ACCEPT_RISK` → create an approval verdict and continue to merge

---

## Files

```text
state/supervisor/config.json
state/supervisor/<issue-id>.json
state/supervisor/escalations/<issue-id>.json
<issue workspace>/.issue-feedback.md
```

---

## Important Rules

1. Structured review JSON is mandatory for machine decisions.
2. `.issue-feedback.md` is the mutable feedback channel.
3. `state/issues/*.json` is authoritative; `state/active-tasks.json` is compatibility output only.
4. Auto-merge requires evaluator pass + review approval + commit match.
