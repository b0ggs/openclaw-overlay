#!/usr/bin/env python3
"""Freeze explicit orchestrator authorization to one or more root epics."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "state"
ISSUES_DIR = STATE_DIR / "issues"
FROZEN_AUTH_PATH = STATE_DIR / "frozen-authorization.json"
USAGE = "scripts/freeze-authorization.py [--replace-inactive] <epic-id> [<epic-id> ...]"
COMPLETE_FROZEN_STATES = {"Human Review", "Done", "Cancelled", "Canceled", "Duplicate"}
OPTIMIZE_INTERNAL_HELPER_SUFFIXES = (
    "exploit",
    "alternative",
    "reset",
    "calibrate",
    "final_validation",
    "submission_bundle",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n")


def restore_file(path: Path, prior_text: str | None) -> None:
    if prior_text is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(prior_text)


def issue_path(issue_id: str) -> Path:
    return ISSUES_DIR / f"{issue_id}.json"


def load_issue(issue_id: str) -> dict[str, Any]:
    path = issue_path(issue_id)
    if not path.exists():
        raise ValueError(f"epic/child not found: {issue_id}")
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        raise ValueError(f"unreadable issue json for {issue_id}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"invalid issue json for {issue_id}")
    return data


def load_all_issues() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(ISSUES_DIR.glob("*.json")):
        try:
            obj = json.loads(path.read_text())
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def unique_ordered(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def internal_optimize_helper_issue_ids(issue: dict[str, Any]) -> list[str]:
    if issue.get("workerMode") != "optimize":
        return []
    if issue.get("optimizeLane") or issue.get("parentOptimizeIssue"):
        return []
    parent_id = str(issue.get("id") or "").strip()
    if not parent_id:
        return []
    return [f"{parent_id}-{suffix}" for suffix in OPTIMIZE_INTERNAL_HELPER_SUFFIXES]


def parent_optimize_issue_id_for_internal_helper(issue_id: str) -> str | None:
    for suffix in OPTIMIZE_INTERNAL_HELPER_SUFFIXES:
        marker = f"-{suffix}"
        if issue_id.endswith(marker):
            parent_id = issue_id[: -len(marker)].strip()
            return parent_id or None
    return None


def validate_freeze_request(epic_ids: list[str]) -> tuple[str, list[str], list[str]]:
    roots = unique_ordered(epic_ids)
    project: str | None = None
    approved_children: set[str] = set()
    approved_internal_helpers: set[str] = set()

    for epic_id in roots:
        epic = load_issue(epic_id)
        if epic.get("kind") != "epic":
            raise ValueError(f"freeze root must be an epic: {epic_id}")

        epic_project = epic.get("project")
        if epic_project in (None, ""):
            raise ValueError(f"epic missing project: {epic_id}")
        if project is None:
            project = str(epic_project)
        elif epic_project != project:
            raise ValueError(
                f"all frozen epics must share one project: {epic_id} has {epic_project}, expected {project}"
            )

        for child_id in epic.get("children") or []:
            child = load_issue(str(child_id))
            child_project = child.get("project")
            if child_project != project:
                raise ValueError(
                    f"declared child {child_id} of {epic_id} is in project {child_project}, expected {project}"
                )
            if child.get("parent") != epic_id:
                raise ValueError(
                    f"declared child {child_id} must have parent {epic_id}; found {child.get('parent')}"
                )
            approved_children.add(str(child_id))
            approved_internal_helpers.update(internal_optimize_helper_issue_ids(child))

    if project is None:
        raise ValueError("no valid epic ids provided")

    return project, roots, sorted(approved_children | approved_internal_helpers)


def render_views() -> None:
    render_state = subprocess.run([str(ROOT / "scripts" / "render-state.py")], check=False)
    if render_state.returncode != 0:
        raise RuntimeError("render-state.py failed")
    render_active = subprocess.run([str(ROOT / "scripts" / "render-active-tasks.py")], check=False)
    if render_active.returncode != 0:
        raise RuntimeError("render-active-tasks.py failed")


def rerender_views_best_effort() -> None:
    subprocess.run([str(ROOT / "scripts" / "render-state.py")], check=False)
    subprocess.run([str(ROOT / "scripts" / "render-active-tasks.py")], check=False)


def active_running_issue_ids() -> list[str]:
    running: list[str] = []
    for issue in load_all_issues():
        iid = str(issue.get("id") or "")
        if iid and issue.get("kind") != "epic" and issue.get("state") == "In Progress":
            running.append(iid)
    return running


def approved_window_complete(approved_issue_ids: list[str]) -> bool:
    if not approved_issue_ids:
        return False
    for issue_id in approved_issue_ids:
        try:
            issue = load_issue(issue_id)
        except ValueError:
            parent_id = parent_optimize_issue_id_for_internal_helper(issue_id)
            if not parent_id:
                return False
            try:
                parent_issue = load_issue(parent_id)
            except ValueError:
                return False
            if parent_issue.get("workerMode") != "optimize":
                return False
            if str(parent_issue.get("state") or "") not in COMPLETE_FROZEN_STATES:
                return False
            continue
        if str(issue.get("state") or "") not in COMPLETE_FROZEN_STATES:
            return False
    return True


def append_freeze_audit(event: dict[str, Any]) -> None:
    path = STATE_DIR / "runs" / "freeze-authorization-audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def frozen_authorization_snapshot(
    orch: dict[str, Any],
    project: str,
    approved_epics: list[str],
    approved_issues: list[str],
    frozen_at: str,
) -> dict[str, Any]:
    snapshot = {
        "project": project,
        "approvedEpicIds": approved_epics,
        "approvedIssueIds": approved_issues,
        "authorizationFrozenAt": frozen_at,
        "authorizationProject": orch.get("authorizationProject") or project,
    }
    for key in ("authorizationScope", "authorizationOriginEpic", "authorizationGrantedAt", "authorizationSource"):
        value = orch.get(key)
        if value not in (None, ""):
            snapshot[key] = value
    return snapshot


def persist_frozen_authorization_snapshot(
    orch: dict[str, Any],
    project: str,
    approved_epics: list[str],
    approved_issues: list[str],
    frozen_at: str,
) -> None:
    save_json(
        FROZEN_AUTH_PATH,
        frozen_authorization_snapshot(orch, project, approved_epics, approved_issues, frozen_at),
    )


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    replace_inactive = False
    filtered_args: list[str] = []
    for arg in args:
        if arg == "--replace-inactive":
            replace_inactive = True
            continue
        filtered_args.append(arg)
    args = filtered_args
    if not args:
        print(f"usage: {USAGE}", file=sys.stderr)
        return 2

    try:
        project, approved_epics, approved_issues = validate_freeze_request(args)
    except ValueError as exc:
        print(f"freeze-authorization.py: {exc}", file=sys.stderr)
        return 1

    orch_path = STATE_DIR / "orchestrator.json"
    orch = load_json(
        orch_path,
        {
            "schemaVersion": 1,
            "activeProject": project,
            "phase": "ready",
            "authorizedEpic": None,
            "maxConcurrentWorkers": 1,
            "runningIssues": [],
            "blockedIssues": [],
        },
    )

    ts = now_iso()
    orch.setdefault("schemaVersion", 1)
    orch["activeProject"] = project

    existing_frozen_at = orch.get("authorizationFrozenAt")
    existing_epics = unique_ordered([str(v) for v in (orch.get("approvedEpicIds") or []) if str(v)])
    existing_issues = unique_ordered([str(v) for v in (orch.get("approvedIssueIds") or []) if str(v)])
    pending_replace_inactive_audit: dict[str, Any] | None = None
    if existing_frozen_at:
        same_window = existing_epics == approved_epics and existing_issues == approved_issues
        if same_window:
            persist_frozen_authorization_snapshot(orch, project, existing_epics, existing_issues, str(existing_frozen_at))
            print(
                json.dumps(
                    {
                        "project": project,
                        "approvedEpicIds": existing_epics,
                        "approvedIssueIds": existing_issues,
                        "authorizationFrozenAt": existing_frozen_at,
                        "uniqueIssuesDispatched": int(orch.get("uniqueIssuesDispatched") or 0),
                        "totalDispatches": int(orch.get("totalDispatches") or 0),
                        "unchanged": True,
                    },
                    indent=2,
                )
            )
            return 0

        running_ids = active_running_issue_ids()
        if not replace_inactive:
            print(
                "freeze-authorization.py: frozen authorization already active; refuse to replace without --replace-inactive",
                file=sys.stderr,
            )
            return 1
        if running_ids:
            print(
                "freeze-authorization.py: cannot replace frozen authorization while issues are running: "
                + ", ".join(running_ids),
                file=sys.stderr,
            )
            return 1
        if not approved_window_complete(existing_issues):
            print(
                "freeze-authorization.py: cannot replace frozen authorization until the current approved window is complete",
                file=sys.stderr,
            )
            return 1
        pending_replace_inactive_audit = {
            "action": "replace_inactive",
            "priorAuthorizationFrozenAt": existing_frozen_at,
            "priorApprovedEpicIds": existing_epics,
            "priorApprovedIssueIds": existing_issues,
            "priorUniqueIssuesDispatched": int(orch.get("uniqueIssuesDispatched") or 0),
            "priorTotalDispatches": int(orch.get("totalDispatches") or 0),
            "nextApprovedEpicIds": approved_epics,
            "nextApprovedIssueIds": approved_issues,
        }

    orch["approvedEpicIds"] = approved_epics
    orch["approvedIssueIds"] = approved_issues
    orch["authorizationFrozenAt"] = ts
    orch["uniqueIssuesDispatched"] = 0
    orch["totalDispatches"] = 0
    orch["lastUpdatedAt"] = ts

    prior_orchestrator = orch_path.read_text() if orch_path.exists() else None
    prior_frozen_snapshot = FROZEN_AUTH_PATH.read_text() if FROZEN_AUTH_PATH.exists() else None

    save_json(orch_path, orch)
    persist_frozen_authorization_snapshot(orch, project, approved_epics, approved_issues, ts)
    try:
        render_views()
    except Exception as exc:
        restore_file(orch_path, prior_orchestrator)
        restore_file(FROZEN_AUTH_PATH, prior_frozen_snapshot)
        rerender_views_best_effort()
        print(
            "freeze-authorization.py: failed to render derived state; restored previous "
            f"orchestrator/frozen authorization files ({exc})",
            file=sys.stderr,
        )
        return 1

    if pending_replace_inactive_audit is not None:
        append_freeze_audit({"timestamp": now_iso(), **pending_replace_inactive_audit})

    print(
        json.dumps(
            {
                "project": project,
                "approvedEpicIds": approved_epics,
                "approvedIssueIds": approved_issues,
                "authorizationFrozenAt": ts,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
