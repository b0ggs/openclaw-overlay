# Subagent Sandbox and Lifecycle Reliability

Status: planning; no fix is implemented by this document.

## Problem

Two related failures make delegated work unsafe and difficult to understand.

### 1. Workspace-lane mismatch

OpenClaw advertises `cwd` as an optional `sessions_spawn` parameter. A model or
orchestrator can therefore request a directory outside the child agent's
configured workspace. When that child is sandboxed, OpenClaw rejects the request
because the requested working directory would escape the sandbox.

The containment rejection is correct. The defect is that delegation can reach
the runtime without an earlier lane check or a clear recovery path.

A common trigger is switching between:

- work on the live harness under the OpenClaw workspace;
- work on repositories under a separate projects root; and
- creation of a new project for a later session.

The orchestrator may retain the old workspace assumption, pass the new path as
`cwd`, and attempt an impossible spawn.

### 2. Unobservable subagent failure

The reported operational behavior is that rejected, failed, or stalled children
are not reliably reported to the user, may remain nonterminal, and may not be
cleaned up. The orchestrator can consequently wait indefinitely, imply progress
that is not occurring, or lose the relationship between a task and its child
run.

The sandbox rejection is a confirmed trigger. The broader hang, cleanup, and
crash sequence still requires a controlled reproduction before its exact failure
mechanism is treated as proven.

## Constraints

- Do not patch the installed OpenClaw distribution. Upgrades would overwrite it.
- Do not weaken or bypass the sandbox containment guard.
- Keep the fix portable through this overlay repository.
- Preserve existing modules and their install/uninstall behavior.
- Do not claim child success without terminal status and required evidence.
- Do not store secrets or full prompts in lifecycle records.

## Proposed Fix

Implement two additive modules. Delegation fails closed if either required
control is unavailable.

### A. Workspace lane guard

Before `sessions_spawn`, resolve and validate:

- the active task root;
- the selected child agent and its configured workspace;
- sandbox mode and workspace access;
- the requested `cwd`, if any; and
- the canonical real paths, including symlink resolution.

Rules:

1. At session start, establish the active work lane from an explicit user choice
   or an unambiguous existing project context.
2. If the lane is unknown or changes materially, ask the user before delegation.
3. Permit a spawn only when the target is contained by the selected agent's
   configured workspace.
4. For same-lane work, omit `cwd` when the agent workspace is already correct.
5. For cross-lane work, select a preconfigured agent whose workspace contains
   the target. Never widen a sandbox implicitly.
6. If no valid agent exists, do not call `sessions_spawn`. Report the requested
   path, selected agent workspace, and safe configuration choices.
7. Require lane selection before delegating creation of a new project.

This turns a runtime rejection into a deterministic preflight result without
changing OpenClaw's guard.

### B. Subagent run supervisor

Wrap every delegated run in a durable lifecycle record:

`requested -> admitted -> running -> succeeded | failed | timed_out | cancelled | orphaned`

Each record should contain only the task reference, parent session, child/run
identifier, selected agent/runtime, workspace lane, timestamps, current state,
and a redacted terminal reason.

Required behavior:

1. Run workspace-lane preflight before recording or spawning.
2. Record the request before `sessions_spawn`.
3. Record the returned child/run identifier or the immediate rejection.
4. Track the child until a terminal state; use bounded polling only when no
   lifecycle event is available.
5. Surface rejection, failure, timeout, and cancellation to the user promptly.
6. On timeout or abandonment, interrupt/cancel the child when supported, release
   associated locks, and mark the final cleanup result.
7. Never translate missing output, malformed output, or a vanished child into
   success.
8. On orchestrator startup, reconcile nonterminal records against live sessions
   and mark or clean up orphans.
9. Keep retries bounded and explicit; a retry receives a new run identifier
   linked to the original attempt.

## Overlay Integration

The intended implementation is additive:

- `workspace-lane-guard`: pure validation plus a small CLI/API seam;
- `subagent-run-supervisor`: lifecycle ledger, reconciliation, and user-visible
  terminal reporting;
- prompt-pack integration only after both runtime modules pass isolated tests.

Existing module source, manifests, checksums, and installed files must remain
unchanged during the first implementation phase. If later prompt-pack changes
are required, they must use its normal manifest/checksum/install tests rather
than editing the live workspace directly.

The modules should consume authoritative OpenClaw configuration through a stable
adapter. Version-specific field discovery belongs in that adapter, allowing a
future OpenClaw upgrade to require an adapter/test update rather than loss of the
fix.

## Test Plan

### Lane guard

- Same workspace with omitted `cwd`: allowed.
- Canonical child path inside workspace: allowed.
- Sibling or parent path outside workspace: blocked before spawn.
- Symlink resolving outside workspace: blocked.
- Cross-project request with a correctly configured agent: routed and allowed.
- Unknown lane or new-project request: user decision required before spawn.
- OpenClaw configuration shape change: fail closed with an actionable error.

### Run supervisor

- Immediate spawn rejection: recorded and reported; no dangling run.
- Successful child: terminal status and evidence are linked to the task.
- Child error or malformed output: failed, never successful.
- Timeout: user notified, cancellation attempted, cleanup outcome recorded.
- Child vanishes: reconciled as orphaned.
- Orchestrator restart: existing nonterminal runs are recovered or closed.
- Duplicate/retry requests: distinct linked attempts, no double counting.

### Compatibility

- Install and uninstall each new module in a disposable workspace.
- Run the complete existing overlay test suite before and after installation.
- Verify existing module manifests and source hashes are unchanged.
- Run a valid delegated task in each supported workspace lane.
- Run an intentional sandbox mismatch and confirm that no child is launched.
- Repeat the tests against a fresh installation of the pinned OpenClaw release,
  then against the candidate upgrade before adopting it.

## Acceptance Criteria

The fix is complete when:

- an invalid workspace request cannot reach `sessions_spawn`;
- the user receives a clear lane mismatch instead of a silent hang;
- every admitted child has a durable parent/task/run relationship;
- every child reaches a reported terminal or reconciled orphan state;
- failed and timed-out children are cleaned up when the runtime supports it;
- restart recovery leaves no unaccounted nonterminal children;
- a clean OpenClaw reinstall plus overlay install restores the behavior; and
- all existing overlay module tests continue to pass unchanged.
