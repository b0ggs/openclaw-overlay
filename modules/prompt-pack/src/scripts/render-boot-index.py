#!/usr/bin/env python3
"""Render and verify the compact OpenClaw boot index.

The index is a generated locator for startup. It is not authority; callers must
read the full JSON records before planning, dispatch, mutation, or publication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = Path("state/boot-index.json")
DEFAULT_TERMINAL_STATES = {"Done", "Cancelled", "Canceled", "Duplicate", "Blocked"}
REGENERATE = "python3 scripts/render-boot-index.py --workspace-root <workspace-root>"


class BootIndexError(RuntimeError):
    """Raised when source state cannot produce a trustworthy boot index."""


def _regenerate_message() -> str:
    return f"Regenerate with: {REGENERATE}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BootIndexError(f"missing required source file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BootIndexError(f"malformed JSON in {path}: {exc.msg} at line {exc.lineno} column {exc.colno}") from exc


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_record(path: Path, workspace_root: Path) -> dict[str, Any]:
    return {
        "path": _display_path(path, workspace_root),
        "sha256": _sha256(path),
    }


def _display_path(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(workspace_root.resolve()))
    except ValueError:
        return str(path)


def _terminal_states(workflow: dict[str, Any] | None) -> set[str]:
    states = (((workflow or {}).get("states") or {}).get("terminal") or [])
    if not isinstance(states, list):
        return set(DEFAULT_TERMINAL_STATES)
    out = {str(item) for item in states if str(item)}
    return out or set(DEFAULT_TERMINAL_STATES)


def _workflow_frontmatter(workspace_root: Path) -> dict[str, Any] | None:
    path = workspace_root / "WORKFLOW.md"
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            try:
                import yaml  # type: ignore
            except ModuleNotFoundError:
                return None
            payload = yaml.safe_load("\n".join(lines[1:index])) or {}
            return payload if isinstance(payload, dict) else None
    return None


def _compact_status(status: Any) -> dict[str, Any] | str | None:
    if isinstance(status, str):
        return _compact_text(status)
    if not isinstance(status, dict):
        return None
    keys = (
        "currentClassification",
        "processState",
    )
    return {key: _compact_text(status[key]) for key in keys if key in status}


def _compact_text(value: Any, limit: int = 100) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def _compat_status(issue: dict[str, Any]) -> str:
    state = str(issue.get("state") or "")
    if state in {"Human Review", "Merging"}:
        return "blocked"
    if state == "Blocked":
        return "blocked"
    if state == "In Progress":
        return "running"
    if state == "Rework":
        return "rework"
    if state == "Todo":
        return "todo"
    return state.lower() or "unknown"


def _allowed_categories(paths: list[Any]) -> list[str]:
    categories: set[str] = set()
    state_paths = {
        "MEMORY.md",
        "STATE.md",
        "state/active-tasks.json",
        "state/orchestrator.json",
    }
    for raw in paths:
        path = str(raw)
        if path in state_paths or path.startswith("state/issues/"):
            categories.add("state/control-plane records")
        elif path.startswith("handoffs/openclaw-overlay-v1-"):
            categories.add("Overlay V1 packet/report/evidence handoff roots")
        elif path:
            categories.add("other issue-scoped paths")
    return sorted(categories)


def _string_list_field(record: dict[str, Any], key: str, context: str) -> list[str]:
    if key not in record:
        raise BootIndexError(f"SCHEMA_MISMATCH: missing {context}.{key}")
    value = record.get(key)
    if not isinstance(value, list):
        raise BootIndexError(f"SCHEMA_MISMATCH: expected {context}.{key} to be a list")
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def _dispatch_authorized_issue_ids(orch: dict[str, Any]) -> list[str]:
    approved_issue_ids = _string_list_field(orch, "approvedIssueIds", "state/orchestrator.json")
    legacy_authorized_work = orch.get("authorizedWork")
    if isinstance(legacy_authorized_work, dict) and "dispatchAuthorizedIssueIds" in legacy_authorized_work:
        legacy_ids = legacy_authorized_work.get("dispatchAuthorizedIssueIds")
        if not isinstance(legacy_ids, list):
            raise BootIndexError(
                "SCHEMA_MISMATCH: expected state/orchestrator.json.authorizedWork.dispatchAuthorizedIssueIds to be a list"
            )
        normalized_legacy_ids: list[str] = []
        seen: set[str] = set()
        for item in legacy_ids:
            text = str(item).strip()
            if text and text not in seen:
                normalized_legacy_ids.append(text)
                seen.add(text)
        if normalized_legacy_ids != approved_issue_ids:
            raise BootIndexError(
                "SCHEMA_MISMATCH: state/orchestrator.json.authorizedWork.dispatchAuthorizedIssueIds "
                "disagrees with live approvedIssueIds"
            )
    return approved_issue_ids


def _issue_pointer(workspace_root: Path, issue_path: Path) -> str:
    return f"state/issues/{issue_path.name}"


def _tracked_issue(workspace_root: Path, issue_path: Path, issue: dict[str, Any]) -> dict[str, Any]:
    issue_id = str(issue.get("id") or issue_path.stem)
    status = issue.get("status") if isinstance(issue.get("status"), dict) else {}
    entry = {
        "id": issue_id,
        "state": issue.get("state"),
        "compatStatus": _compat_status(issue),
        "classification": (status or {}).get("currentClassification"),
        "compactStatus": _compact_status(status),
        "pointer": _issue_pointer(workspace_root, issue_path),
    }
    return {key: value for key, value in entry.items() if value not in (None, {}, [])}


def build_index(workspace_root: Path, *, generated_at: str | None = None) -> dict[str, Any]:
    workspace_root = workspace_root.resolve()
    state_dir = workspace_root / "state"
    issues_dir = state_dir / "issues"
    orch_path = state_dir / "orchestrator.json"
    orch = _json_load(orch_path)
    if not isinstance(orch, dict):
        raise BootIndexError(f"malformed orchestrator state in {orch_path}: expected object")

    workflow = _workflow_frontmatter(workspace_root)
    terminal_states = _terminal_states(workflow)
    issue_paths = sorted(issues_dir.glob("*.json")) if issues_dir.exists() else []
    source_files = [_source_record(orch_path, workspace_root)]
    tracked: list[dict[str, Any]] = []
    authorized: list[dict[str, Any]] = []
    allowed: list[dict[str, Any]] = []
    dispatch_authorized_issue_ids = _dispatch_authorized_issue_ids(orch)
    dispatch_authorized_issue_id_set = set(dispatch_authorized_issue_ids)

    for issue_path in issue_paths:
        issue = _json_load(issue_path)
        if not isinstance(issue, dict):
            raise BootIndexError(f"malformed issue state in {issue_path}: expected object")
        source_files.append(_source_record(issue_path, workspace_root))
        if str(issue.get("state") or "") in terminal_states:
            continue
        tracked.append(_tracked_issue(workspace_root, issue_path, issue))
        allowed_paths = issue.get("allowedPaths") if isinstance(issue.get("allowedPaths"), list) else []
        if allowed_paths:
            allowed_entry = {
                "issueId": str(issue.get("id") or issue_path.stem),
                "categories": _allowed_categories(allowed_paths),
                "allowedPathsPointer": f"{_issue_pointer(workspace_root, issue_path)}.allowedPaths",
            }
            if allowed_entry["issueId"] in dispatch_authorized_issue_id_set:
                authorized.append({**allowed_entry, "dispatchAuthorized": True})
            else:
                allowed.append({**allowed_entry, "dispatchAuthorized": False})

    index = {
        "schemaVersion": 1,
        "lastIndexedAt": generated_at or _utc_now(),
        "activeProject": orch.get("activeProject"),
        "phase": orch.get("phase"),
        "status": _compact_text(orch.get("status") or ""),
        "activeIssueIds": orch.get("activeIssueIds") or [],
        "approvedIssueIds": orch.get("approvedIssueIds") or [],
        "approvedEpicIds": orch.get("approvedEpicIds") or [],
        "authorizedEpic": orch.get("authorizedEpic"),
        "authorizationScope": orch.get("authorizationScope"),
        "runningIssues": orch.get("runningIssues") or [],
        "blockedIssueIds": orch.get("blockedIssueIds") or orch.get("blockedIssues") or [],
        "trackedIssues": tracked,
        "authorizedWork": {
            "dispatchAuthorizedIssueIds": dispatch_authorized_issue_ids,
            "trackedDispatchAuthorized": authorized,
            "trackedButNotDispatchAuthorized": allowed,
        },
        "pointers": {
            "orchestratorState": "state/orchestrator.json",
            "issueRecords": "state/issues/*.json",
            "derivedCompatibilityView": "state/active-tasks.json",
            "fullWorkflowContract": "docs/on-demand/WORKFLOW.full.md",
            "fullRoleAndPolicyDetail": "docs/on-demand/ORCHESTRATOR.full.md",
            "bootDetails": "docs/on-demand/BOOT.details.md",
            "currentBootShell": "BOOT.md",
        },
        "_meta": {
            "sourceFiles": source_files,
            "regenerateInstruction": REGENERATE,
        },
    }
    return index


def write_index(index: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(index, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def check_index(workspace_root: Path, index_path: Path) -> None:
    if not index_path.exists():
        raise BootIndexError(f"missing generated boot index: {index_path}. {_regenerate_message()}")
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BootIndexError(f"malformed generated boot index: {index_path}: {exc.msg}. {_regenerate_message()}") from exc

    current = build_index(workspace_root, generated_at=index.get("lastIndexedAt") or "check")
    expected_sources = ((index.get("_meta") or {}).get("sourceFiles") or []) if isinstance(index, dict) else []
    current_sources = (current.get("_meta") or {}).get("sourceFiles") or []
    if expected_sources != current_sources:
        raise BootIndexError(f"stale generated boot index: {index_path}. {_regenerate_message()}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, default=ROOT, help="Workspace root containing state/")
    parser.add_argument("--output", type=Path, default=None, help="Index output path")
    parser.add_argument("--check", action="store_true", help="Verify the existing index is present and current")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    workspace_root = args.workspace_root.resolve()
    output = args.output or (workspace_root / DEFAULT_OUTPUT)
    try:
        if args.check:
            check_index(workspace_root, output)
        else:
            write_index(build_index(workspace_root), output)
    except BootIndexError as exc:
        print(f"BOOT_INDEX_ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
