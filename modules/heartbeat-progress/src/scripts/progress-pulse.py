#!/usr/bin/env python3
"""Emit terse user-facing progress updates for autonomous runs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research_runtime import champion_metric, load_research_context

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "state"
TRACKER_PATH = ROOT / "memory" / "progress-pulse-state.json"
STALE_MINUTES = 10


def now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now().isoformat()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n")


def parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def minutes_since(raw: str | None) -> float | None:
    ts = parse_iso(raw)
    if ts is None:
        return None
    return (now() - ts).total_seconds() / 60.0


def load_issues() -> list[dict[str, Any]]:
    issues_dir = STATE_DIR / "issues"
    if not issues_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(issues_dir.glob("*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except Exception:
            out.append({"id": p.stem, "state": "Blocked", "status": {"blockedReason": "unreadable issue json"}})
    return out


def load_self_improvement_candidates() -> list[dict[str, Any]]:
    candidates_dir = STATE_DIR / "self_improvement" / "candidates"
    if not candidates_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(candidates_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def latest_promotion_event() -> dict[str, Any] | None:
    path = STATE_DIR / "self_improvement" / "promotions.jsonl"
    if not path.exists():
        return None
    last: dict[str, Any] | None = None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        if isinstance(data, dict):
            last = data
    return last


def is_required(issue: dict[str, Any]) -> bool:
    return issue.get("required", True) is not False


def issue_label(issue: dict[str, Any]) -> str:
    iid = issue.get("id") or "(unknown)"
    title = (issue.get("title") or "").strip()
    if title and title.lower() != iid.lower():
        short = title[:80]
        return f"{iid} ({short})"
    return iid


def issue_sort_key(issue: dict[str, Any]) -> tuple[Any, ...]:
    status = issue.get("status") or {}
    return (
        status.get("lastDispatchedAt") or "",
        status.get("firstDispatchedAt") or "",
        issue.get("id") or "",
    )


def epic_child_tasks(epic_id: str, epic: dict[str, Any], issues_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    child_ids = set(epic.get("children") or [])
    child_ids.update(
        cid for cid, child in issues_by_id.items()
        if child.get("parent") == epic_id and child.get("kind") != "epic"
    )
    tasks = [issues_by_id[cid] for cid in child_ids if cid in issues_by_id and issues_by_id[cid].get("kind") != "epic"]
    tasks.sort(key=lambda issue: issue.get("id") or "")
    return tasks


def research_ctx_for_issue(issue: dict[str, Any]) -> dict[str, Any]:
    project = str(issue.get("project") or "")
    if not project:
        return {}
    return load_research_context(project)


def issue_lane_role(issue: dict[str, Any]) -> str | None:
    worker_mode = str(issue.get("workerMode") or "")
    if worker_mode == "optimize":
        role = str(issue.get("laneRole") or "").strip()
        if role:
            return role
        if str(issue.get("researchAction") or "candidate_generation") == "candidate_generation":
            return "exploit"
        return str(issue.get("researchAction") or "optimize")
    if worker_mode == "bridge_research" and issue.get("researchCritic"):
        return "critic"
    return None


def active_research_issue(running: list[dict[str, Any]]) -> dict[str, Any] | None:
    for issue in running:
        if issue.get("workerMode") == "optimize":
            return issue
    for issue in running:
        if issue.get("workerMode") == "bridge_research" and issue.get("researchCritic"):
            return issue
    return None


def active_lane_roles(project: str, issues: list[dict[str, Any]]) -> list[str]:
    roles: list[str] = []
    for issue in issues:
        if issue.get("project") != project:
            continue
        if issue.get("state") != "In Progress":
            continue
        role = issue_lane_role(issue)
        if role:
            roles.append(role)
    return sorted(dict.fromkeys(roles))


def budget_pct(remaining: Any, total: Any) -> int | None:
    try:
        rem = float(remaining)
        tot = float(total)
    except Exception:
        return None
    if tot <= 0:
        return None
    return round((rem / tot) * 100)


def build_context() -> dict[str, Any]:
    orch = load_json(STATE_DIR / "orchestrator.json", {}) or {}
    issues = load_issues()
    candidates = load_self_improvement_candidates()
    issues_by_id = {issue.get("id"): issue for issue in issues if issue.get("id")}

    blocked = [issue for issue in issues if issue.get("state") == "Blocked" and issue.get("kind") != "epic"]
    running = [issue for issue in issues if issue.get("state") == "In Progress" and issue.get("kind") != "epic"]
    blocked.sort(key=issue_sort_key)
    running.sort(key=issue_sort_key)

    research_issue = active_research_issue(running)
    if research_issue:
        current = research_issue
        rctx = research_ctx_for_issue(current)
        state = rctx.get("state") or {}
        manifest = rctx.get("manifest") or {}
        budget = rctx.get("budget") or {}
        frontier = rctx.get("frontier") or {}
        last = state.get("lastMeaningfulProgress") or {}
        phase = state.get("phase") or "search"
        metric = champion_metric(manifest)
        strategy = orch.get("researchStrategy") or ("finalization" if phase == "submission_freeze" else "single-lane")
        lane_roles = active_lane_roles(str(current.get("project") or ""), issues)
        signature = {
            "kind": "research-active",
            "project": current.get("project"),
            "phase": phase,
            "event": last.get("event"),
            "eventTs": last.get("timestamp"),
            "championId": manifest.get("candidate_id"),
            "metric": metric,
            "truth": state.get("truthStatus"),
            "laneRoles": lane_roles,
            "strategy": strategy,
        }
        if phase == "blocked_truth_unavailable" or state.get("truthStatus") == "unavailable":
            return {
                "kind": "research-blocked",
                "project": current.get("project"),
                "issueId": current.get("id"),
                "issueLabel": issue_label(current),
                "phase": phase,
                "truthStatus": state.get("truthStatus"),
                "championId": manifest.get("candidate_id"),
                "strategy": strategy,
                "signature": signature,
            }
        if phase == "blocked_flatline":
            return {
                "kind": "research-flatline",
                "project": current.get("project"),
                "issueId": current.get("id"),
                "issueLabel": issue_label(current),
                "phase": phase,
                "championId": manifest.get("candidate_id"),
                "championMetric": metric,
                "laneRoles": lane_roles,
                "strategy": strategy,
                "signature": signature,
            }
        return {
            "kind": "research-active",
            "project": current.get("project"),
            "issueId": current.get("id"),
            "issueLabel": issue_label(current),
            "phase": phase,
            "championId": manifest.get("candidate_id"),
            "championMetric": metric,
            "championConfidence": manifest.get("confidence"),
            "budgetRemaining": budget.get("remainingEvaluations"),
            "budgetTotal": budget.get("totalEvaluations"),
            "budgetPct": budget_pct(budget.get("remainingEvaluations"), budget.get("totalEvaluations")),
            "lastEvent": last,
            "laneRoles": lane_roles,
            "laneCount": len(lane_roles),
            "frontierCount": len(frontier.get("candidates") or []),
            "frontierMax": frontier.get("maxCandidates"),
            "strategy": strategy,
            "signature": signature,
        }

    if blocked and (orch.get("phase") == "blocked" or not running):
        issue = blocked[0]
        return {
            "kind": "blocked",
            "project": issue.get("project") or orch.get("activeProject"),
            "issueId": issue.get("id"),
            "issueLabel": issue_label(issue),
            "blockedReason": (issue.get("status") or {}).get("blockedReason") or "blocked",
            "signature": {
                "kind": "blocked",
                "issueId": issue.get("id"),
                "reason": (issue.get("status") or {}).get("blockedReason") or "blocked",
            },
        }

    if running:
        current = running[0]
        epic_id = orch.get("authorizedEpic") or current.get("parent")
        epic = issues_by_id.get(epic_id) if epic_id else None
        required_children: list[dict[str, Any]] = []
        if epic is not None:
            required_children = [task for task in epic_child_tasks(epic_id, epic, issues_by_id) if is_required(task)]
        done_count = sum(1 for task in required_children if task.get("state") == "Done")
        total_count = len(required_children) or None
        current_index = min(done_count + 1, total_count) if total_count else None
        return {
            "kind": "active",
            "project": current.get("project") or orch.get("activeProject"),
            "epicId": epic_id,
            "epicTitle": (epic or {}).get("title"),
            "issueId": current.get("id"),
            "issueLabel": issue_label(current),
            "doneCount": done_count,
            "totalCount": total_count,
            "currentIndex": current_index,
            "signature": {
                "kind": "active",
                "epicId": epic_id,
                "issueId": current.get("id"),
                "doneCount": done_count,
                "totalCount": total_count,
            },
        }

    running_candidate = next((candidate for candidate in candidates if str(candidate.get("trialState") or "") == "replay_running"), None)
    if running_candidate is not None:
        cid = running_candidate.get("id") or "(unknown)"
        replay_set = ((running_candidate.get("replayPlan") or {}).get("replaySetId") or "(none)")
        return {
            "kind": "candidate-running",
            "candidateId": cid,
            "replaySetId": replay_set,
            "rolloutState": running_candidate.get("rolloutState") or "shadow_only",
            "signature": {
                "kind": "candidate-running",
                "candidateId": cid,
                "trialState": running_candidate.get("trialState"),
                "rolloutState": running_candidate.get("rolloutState"),
            },
        }

    promotion = latest_promotion_event()
    if promotion is not None:
        outcome = str(promotion.get("newStatus") or promotion.get("outcome") or promotion.get("rolloutState") or promotion.get("status") or "").strip()
        if outcome:
            return {
                "kind": "candidate-event",
                "candidateId": promotion.get("candidateId") or promotion.get("id"),
                "outcome": outcome,
                "summary": promotion.get("summary") or promotion.get("reason"),
                "signature": {
                    "kind": "candidate-event",
                    "candidateId": promotion.get("candidateId") or promotion.get("id"),
                    "outcome": outcome,
                    "at": promotion.get("timestamp") or promotion.get("updatedAt"),
                },
            }

    completed_candidate = next(
        (
            candidate for candidate in candidates
            if str(candidate.get("trialState") or "") == "replay_completed"
            and (STATE_DIR / "self_improvement" / "comparisons" / f"{candidate.get('id')}.json").exists()
        ),
        None,
    )
    if completed_candidate is not None:
        cid = completed_candidate.get("id") or "(unknown)"
        return {
            "kind": "candidate-complete",
            "candidateId": cid,
            "rolloutState": completed_candidate.get("rolloutState") or "shadow_only",
            "signature": {
                "kind": "candidate-complete",
                "candidateId": cid,
                "rolloutState": completed_candidate.get("rolloutState"),
                "updatedAt": completed_candidate.get("updatedAt"),
            },
        }

    status = str(orch.get("status") or "")
    phase = str(orch.get("phase") or "")
    if phase == "ready" and "Authorized scope complete" in status:
        return {
            "kind": "complete",
            "project": orch.get("activeProject"),
            "status": status,
            "signature": {
                "kind": "complete",
                "project": orch.get("activeProject"),
                "status": status,
            },
        }

    return {
        "kind": "idle",
        "project": orch.get("activeProject"),
        "status": status,
        "signature": {"kind": "idle", "status": status},
    }


def build_research_message(ctx: dict[str, Any], state: dict[str, Any]) -> str | None:
    last_sig = state.get("lastSignature")
    quiet_for = minutes_since(state.get("lastSentAt"))
    kind = ctx["kind"]

    if kind == "research-blocked":
        if last_sig != ctx["signature"]:
            champ = ctx.get("championId") or "current champion"
            return f"Truth unavailable for {ctx['project']}; search paused on {champ}."
        return None

    if kind == "research-flatline":
        if last_sig != ctx["signature"]:
            champ = ctx.get("championId") or "current champion"
            metric = ctx.get("championMetric")
            if metric is not None:
                return f"Flatline reached for {ctx['project']} on {champ} at {metric}; shifting to distillation."
            return f"Flatline reached for {ctx['project']} on {champ}; shifting to distillation."
        return None

    if kind != "research-active":
        return None

    last_event = ctx.get("lastEvent") or {}
    last_event_name = last_event.get("event")
    lane_roles = ctx.get("laneRoles") or []
    lane_text = f"{len(lane_roles)} lanes active ({', '.join(lane_roles)})" if lane_roles else "single lane active"
    strategy = ctx.get("strategy") or "single-lane"
    if last_sig != ctx["signature"]:
        if last_event_name == "champion_replacement":
            return (
                f"New champion for {ctx['project']}: {ctx.get('championId')} at {ctx.get('championMetric')}. "
                f"{lane_text}; strategy {strategy}."
            )
        if last_event_name in {"deadline_freeze", "phase_transition"} and ctx.get("phase") == "submission_freeze":
            return f"Entered submission freeze for {ctx['project']} — verifying the champion."
        if last_event_name == "dead_end_confirmed":
            return f"Confirmed a dead end for {ctx['project']} — continuing from the current champion."
        if last_event_name == "truth_restored":
            return f"Truth restored for {ctx['project']} — resuming search."
        if last_event_name == "calibration_completed":
            return f"Calibration completed for {ctx['project']} — continuing search."
        if last_event_name == "submission_bundle_verified":
            return f"Submission bundle verified for {ctx['project']}."
        if last_event_name == "budget_exhausted":
            return f"Evaluation budget exhausted for {ctx['project']} — reporting the best champion."
        if ctx.get("phase") == "search":
            return (
                f"{ctx['project']}: {lane_text}. Champion {ctx.get('championId')} at {ctx.get('championMetric')}. "
                f"Strategy {strategy}."
            )

    if quiet_for is not None and quiet_for >= STALE_MINUTES:
        remaining = ctx.get("budgetRemaining")
        total = ctx.get("budgetTotal")
        pct = ctx.get("budgetPct")
        budget_text = f"{pct}% budget remaining" if pct is not None else (
            f"{remaining}/{total} evals remaining" if remaining is not None and total is not None else "budget not configured"
        )
        frontier_count = ctx.get("frontierCount")
        frontier_max = ctx.get("frontierMax")
        frontier_text = (
            f"frontier {frontier_count}/{frontier_max}" if frontier_count is not None and frontier_max is not None else None
        )
        details = [lane_text, f"champion {ctx.get('championId')} at {ctx.get('championMetric')}", budget_text]
        if frontier_text:
            details.append(frontier_text)
        return (
            f"{ctx['project']}: " + ". ".join(details) + f". Strategy {strategy}."
        )
    return None


def build_message(ctx: dict[str, Any], state: dict[str, Any]) -> str | None:
    last_sig = state.get("lastSignature")
    last_issue = state.get("lastIssueId")
    last_done = state.get("lastDoneCount")
    last_kind = state.get("lastKind")
    quiet_for = minutes_since(state.get("lastSentAt"))

    kind = ctx["kind"]
    if kind == "idle":
        return None

    if kind in {"research-active", "research-blocked", "research-flatline"}:
        return build_research_message(ctx, state)

    if kind == "blocked":
        if last_sig != ctx["signature"]:
            return f"Blocked on {ctx['issueLabel']}: {ctx['blockedReason']}"
        return None

    if kind == "complete":
        if last_sig != ctx["signature"] or last_kind != "complete":
            project = ctx.get("project") or "the authorized scope"
            return f"Authorized scope complete for {project} — awaiting your review."
        return None

    if kind == "candidate-running":
        if last_sig != ctx["signature"]:
            return f"Harness replay started for {ctx['candidateId']} on replay set {ctx['replaySetId']} — continuing."
        if quiet_for is not None and quiet_for >= STALE_MINUTES:
            return f"Still replaying harness candidate {ctx['candidateId']} ({ctx['rolloutState']}) — continuing."
        return None

    if kind == "candidate-complete":
        if last_sig != ctx["signature"]:
            return f"Harness replay completed for {ctx['candidateId']} — comparison artifact is ready."
        return None

    if kind == "candidate-event":
        if last_sig != ctx["signature"]:
            summary = str(ctx.get("summary") or "").strip()
            suffix = f" {summary}" if summary else ""
            return f"Harness candidate {ctx['candidateId']} -> {ctx['outcome']}.{suffix}".strip()
        return None

    done_count = int(ctx.get("doneCount") or 0)
    total_count = ctx.get("totalCount")
    current_index = ctx.get("currentIndex")
    issue_label = ctx["issueLabel"]

    if last_kind != "active":
        if total_count:
            return f"Working through task {current_index}/{total_count}: {issue_label} — continuing."
        return f"Working on {issue_label} — continuing."

    if isinstance(last_done, int) and done_count > last_done:
        if total_count:
            return f"Task {done_count}/{total_count} completed — continuing with {issue_label}."
        return f"A task completed — continuing with {issue_label}."

    if issue_label and issue_label != last_issue:
        if total_count:
            return f"Now working on task {current_index}/{total_count}: {issue_label} — continuing."
        return f"Now working on {issue_label} — continuing."

    if quiet_for is not None and quiet_for >= STALE_MINUTES:
        if total_count:
            return f"Still working on task {current_index}/{total_count}: {issue_label}. {done_count}/{total_count} completed so far — continuing."
        return f"Still working on {issue_label} — continuing."

    return None


def main() -> int:
    state = load_json(TRACKER_PATH, {}) or {}
    ctx = build_context()
    msg = build_message(ctx, state)

    if not msg:
        print("NO_UPDATE")
        return 0

    new_state = {
        "version": 1,
        "lastSentAt": now_iso(),
        "lastKind": ctx.get("kind"),
        "lastIssueId": ctx.get("issueLabel") if ctx.get("kind") in {"active", "research-active", "research-blocked", "research-flatline"} else ctx.get("issueId"),
        "lastDoneCount": ctx.get("doneCount"),
        "lastSignature": ctx.get("signature"),
        "lastProject": ctx.get("project"),
    }
    save_json(TRACKER_PATH, new_state)
    print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
