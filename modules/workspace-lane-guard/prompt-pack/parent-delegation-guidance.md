# Workspace-Lane Delegation Guidance

The parent is trusted as control-plane-equivalent. These rules are operating
discipline, not a security boundary against a deliberately bypassing parent.

- Select a lane through the authenticated owner/operator workflow.
- Delegate only with native `sessions_spawn`.
- Never self-issue, widen, clear, or force-clear a lane.
- Never edit or run commands in a workspace with an active reservation.
- Never use `openclaw agent`, `openclaw agent --local`, or another delegation
  bypass.
- Use `sessions_yield` when the child result is required.
- Do not automatically retry a conflict or quarantine. Yield, or select a
  genuinely disjoint workspace.

The guard applies only while its trusted policy is loaded. The external health
monitor makes absence or unready state loud; it does not make plugin absence
fail closed.
