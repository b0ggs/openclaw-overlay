from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DispatchHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "state" / "issues").mkdir(parents=True, exist_ok=True)
        (self.root / "state" / "runs").mkdir(parents=True, exist_ok=True)
        self.write_workflow()
        (self.root / "projects" / "proj").mkdir(parents=True, exist_ok=True)

        self.tick = load_module("test_orchestrator_tick", REPO_ROOT / "scripts" / "orchestrator-tick.py")
        self.freeze = load_module("test_freeze_authorization", REPO_ROOT / "scripts" / "freeze-authorization.py")
        self.tick.validate_quarantine_allowed_paths.__globals__["ROOT"] = self.root
        self.tick.validate_quarantine_project_root.__globals__["PROJECTS_DIR"] = self.root / "projects"

        for module in (self.tick, self.freeze):
            module.ROOT = self.root
            module.STATE_DIR = self.root / "state"
            if hasattr(module, "FROZEN_AUTH_PATH"):
                module.FROZEN_AUTH_PATH = self.root / "state" / "frozen-authorization.json"
        self.freeze.ISSUES_DIR = self.root / "state" / "issues"
        self.stop_path = self.root / ".openclaw" / "STOP"
        self.tick.stop_file = lambda: self.stop_path

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_workflow(self, max_unique: int = 2, max_total: int = 5, multi_worker_enabled: bool = False) -> None:
        (self.root / "WORKFLOW.md").write_text(
            """---
agents:
  max_concurrent: 1
autonomy:
  default_max_required_children: 20
  allow_project_scope_blanket_go: true
  max_unique_issues_per_window: %d
  max_total_dispatches_per_window: %d
quarantine:
  multi_worker:
    enabled: %s
---
workflow
"""
            % (max_unique, max_total, "true" if multi_worker_enabled else "false")
        )

    def write_json(self, relative_path: str, obj: dict) -> None:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, indent=2) + "\n")

    def read_json(self, relative_path: str) -> dict:
        return json.loads((self.root / relative_path).read_text())

    def write_frozen_authorization(self, **overrides) -> dict:
        snapshot = {
            "project": "proj",
            "approvedEpicIds": [],
            "approvedIssueIds": [],
            "authorizationFrozenAt": None,
            "authorizationProject": "proj",
        }
        snapshot.update(overrides)
        self.write_json("state/frozen-authorization.json", snapshot)
        return snapshot

    def write_issue(self, issue_id: str, **fields) -> None:
        issue = {
            "id": issue_id,
            "project": fields.pop("project", "proj"),
            "title": fields.pop("title", issue_id),
            "state": fields.pop("state", "Todo"),
            "kind": fields.pop("kind", "task"),
            "workerMode": fields.pop("workerMode", "code"),
        }
        issue.update(fields)
        self.write_json(f"state/issues/{issue_id}.json", issue)

    def test_invalid_done_dependency_does_not_unlock_successor(self) -> None:
        done_dep = {
            "id": "dep-invalid-done",
            "project": "proj",
            "title": "invalid dependency",
            "state": "Done",
            "kind": "task",
            "required": True,
            "finalizerRequired": True,
            "allowedPaths": ["scripts/finalizer_required.py"],
            "status": {
                "finalizer": {
                    "ok": True,
                    "status": "passed",
                    "subjectCommitOid": "abc123",
                    "destinationRef": "refs/heads/main",
                    "changedPaths": ["scripts/finalizer_required.py"],
                    "stagedPaths": ["scripts/finalizer_required.py"],
                    "errors": [],
                }
            },
        }
        successor = {
            "id": "successor",
            "project": "proj",
            "title": "successor",
            "state": "Todo",
            "kind": "task",
            "dependsOn": ["dep-invalid-done"],
        }
        epic = {
            "id": "epic-with-invalid-child",
            "project": "proj",
            "title": "epic",
            "state": "Todo",
            "kind": "epic",
            "children": ["dep-invalid-done"],
        }
        issues = {
            "dep-invalid-done": done_dep,
            "successor": successor,
            "epic-with-invalid-child": epic,
        }

        self.assertFalse(self.tick.issue_finalizer_allows_completion(done_dep, "Done"))
        self.assertFalse(self.tick.dependency_satisfied("dep-invalid-done", issues))
        self.assertFalse(self.tick.eligible(successor, issues))
        self.assertFalse(self.tick.all_required_children_done("epic-with-invalid-child", epic, issues))
        self.assertTrue(self.tick.epic_has_open_required_children("epic-with-invalid-child", epic, issues))
        self.assertFalse(self.tick.dependency_satisfied("epic-with-invalid-child", issues))

    def write_orchestrator(self, **overrides) -> dict:
        orch = {
            "schemaVersion": 1,
            "activeProject": "proj",
            "phase": "ready",
            "authorizedEpic": None,
            "maxConcurrentWorkers": 1,
            "runningIssues": [],
            "blockedIssues": [],
            "approvedEpicIds": [],
            "approvedIssueIds": [],
            "authorizationFrozenAt": None,
            "uniqueIssuesDispatched": 0,
            "totalDispatches": 0,
            "lastUpdatedAt": "2026-03-29T00:00:00Z",
            "status": "baseline",
        }
        orch.update(overrides)
        self.write_json("state/orchestrator.json", orch)
        if orch.get("authorizationFrozenAt") and orch.get("approvedEpicIds") and orch.get("approvedIssueIds"):
            self.write_frozen_authorization(
                project=orch.get("activeProject") or "proj",
                approvedEpicIds=orch.get("approvedEpicIds") or [],
                approvedIssueIds=orch.get("approvedIssueIds") or [],
                authorizationFrozenAt=orch.get("authorizationFrozenAt"),
                authorizationProject=orch.get("authorizationProject") or orch.get("activeProject") or "proj",
                authorizationScope=orch.get("authorizationScope"),
                authorizationOriginEpic=orch.get("authorizationOriginEpic"),
                authorizationGrantedAt=orch.get("authorizationGrantedAt"),
                authorizationSource=orch.get("authorizationSource"),
            )
        return orch

    def fake_subprocess_run(self, calls: list[list[str]]):
        def _run(cmd, *args, **kwargs):
            if isinstance(cmd, (list, tuple)):
                rendered = [str(part) for part in cmd]
            else:
                rendered = [str(cmd)]
            calls.append(rendered)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        return _run

    def test_freeze_requires_explicit_epic_ids(self) -> None:
        baseline = self.write_orchestrator(status="unchanged")
        stderr = io.StringIO()
        with mock.patch.object(self.freeze, "render_views", return_value=None), redirect_stderr(stderr):
            rc = self.freeze.main([])

        self.assertNotEqual(rc, 0)
        self.assertIn("usage:", stderr.getvalue())
        self.assertEqual(self.read_json("state/orchestrator.json"), baseline)

    def test_freeze_only_uses_explicit_roots_and_declared_children(self) -> None:
        self.write_orchestrator()
        self.write_issue("epic-a", kind="epic", children=["task-a1", "epic-child"], state="Todo")
        self.write_issue("task-a1", parent="epic-a")
        self.write_issue("epic-child", kind="epic", parent="epic-a", children=["task-nested"], state="Todo")
        self.write_issue("task-nested", parent="epic-child")
        self.write_issue("task-parent-only", parent="epic-a")
        self.write_issue(
            "epic-auto",
            kind="epic",
            children=["task-auto"],
            autonomy={"granted": True, "scope": "end_to_end"},
            status={"authorizedAt": "2026-03-29T01:00:00Z", "authorizationSource": "issue json"},
            state="Todo",
        )
        self.write_issue("task-auto", parent="epic-auto")
        self.write_issue("epic-b", kind="epic", children=["task-b1"], state="Todo")
        self.write_issue("task-b1", parent="epic-b")

        stdout = io.StringIO()
        with mock.patch.object(self.freeze, "render_views", return_value=None), redirect_stdout(stdout):
            rc = self.freeze.main(["epic-a", "epic-b"])

        self.assertEqual(rc, 0)
        orch = self.read_json("state/orchestrator.json")
        self.assertEqual(orch["approvedEpicIds"], ["epic-a", "epic-b"])
        self.assertEqual(orch["approvedIssueIds"], ["epic-child", "task-a1", "task-b1"])
        self.assertNotIn("task-parent-only", orch["approvedIssueIds"])
        self.assertNotIn("task-nested", orch["approvedIssueIds"])
        self.assertNotIn("epic-auto", orch["approvedEpicIds"])
        self.assertNotIn("task-auto", orch["approvedIssueIds"])
        printed = json.loads(stdout.getvalue())
        self.assertEqual(printed["approvedIssueIds"], ["epic-child", "task-a1", "task-b1"])

    def test_freeze_predeclares_internal_optimize_helper_issue_ids(self) -> None:
        self.write_issue("epic-a", kind="epic", project="proj", children=["opt-parent"], state="Todo")
        self.write_issue(
            "opt-parent",
            project="proj",
            parent="epic-a",
            workerMode="optimize",
            researchAction="candidate_generation",
            state="Todo",
        )

        stdout = io.StringIO()
        with mock.patch.object(self.freeze, "render_views", return_value=None), redirect_stdout(stdout):
            rc = self.freeze.main(["epic-a"])

        self.assertEqual(rc, 0)
        orch = self.read_json("state/orchestrator.json")
        approved = set(orch["approvedIssueIds"])
        self.assertTrue(
            {
                "opt-parent",
                "opt-parent-exploit",
                "opt-parent-alternative",
                "opt-parent-reset",
                "opt-parent-calibrate",
                "opt-parent-final_validation",
                "opt-parent-submission_bundle",
            }.issubset(approved)
        )
        printed = json.loads(stdout.getvalue())
        self.assertEqual(set(printed["approvedIssueIds"]), approved)

    def test_freeze_replace_inactive_allows_missing_predeclared_helper_issue_ids(self) -> None:
        self.write_orchestrator(
            phase="ready",
            approvedEpicIds=["epic-a"],
            approvedIssueIds=[
                "opt-parent",
                "opt-parent-exploit",
                "opt-parent-alternative",
                "opt-parent-reset",
                "opt-parent-calibrate",
                "opt-parent-final_validation",
                "opt-parent-submission_bundle",
            ],
            authorizationFrozenAt="2026-03-29T01:00:00Z",
        )
        self.write_issue("epic-a", kind="epic", children=["opt-parent"], state="Done")
        self.write_issue(
            "opt-parent",
            parent="epic-a",
            state="Done",
            workerMode="optimize",
            researchAction="candidate_generation",
        )
        self.write_issue("epic-b", kind="epic", children=["task-b1"], state="Todo")
        self.write_issue("task-b1", parent="epic-b", state="Todo")

        stdout = io.StringIO()
        with mock.patch.object(self.freeze, "render_views", return_value=None), redirect_stdout(stdout):
            rc = self.freeze.main(["--replace-inactive", "epic-b"])

        self.assertEqual(rc, 0)
        orch = self.read_json("state/orchestrator.json")
        self.assertEqual(orch["approvedEpicIds"], ["epic-b"])
        self.assertEqual(orch["approvedIssueIds"], ["task-b1"])
        printed = json.loads(stdout.getvalue())
        self.assertEqual(printed["approvedEpicIds"], ["epic-b"])
        self.assertEqual(printed["approvedIssueIds"], ["task-b1"])

    def test_freeze_replace_inactive_rejects_missing_helper_issue_ids_when_parent_incomplete(self) -> None:
        baseline = self.write_orchestrator(
            phase="ready",
            approvedEpicIds=["epic-a"],
            approvedIssueIds=[
                "opt-parent",
                "opt-parent-exploit",
                "opt-parent-alternative",
                "opt-parent-reset",
                "opt-parent-calibrate",
                "opt-parent-final_validation",
                "opt-parent-submission_bundle",
            ],
            authorizationFrozenAt="2026-03-29T01:00:00Z",
        )
        self.write_issue("epic-a", kind="epic", children=["opt-parent"], state="Todo")
        self.write_issue(
            "opt-parent",
            parent="epic-a",
            state="Todo",
            workerMode="optimize",
            researchAction="candidate_generation",
        )
        self.write_issue("epic-b", kind="epic", children=["task-b1"], state="Todo")
        self.write_issue("task-b1", parent="epic-b", state="Todo")

        stderr = io.StringIO()
        with mock.patch.object(self.freeze, "render_views", return_value=None), redirect_stderr(stderr):
            rc = self.freeze.main(["--replace-inactive", "epic-b"])

        self.assertNotEqual(rc, 0)
        self.assertIn("current approved window is complete", stderr.getvalue())
        self.assertEqual(self.read_json("state/orchestrator.json"), baseline)

    def test_freeze_rejects_mid_run_replacement_without_explicit_replace(self) -> None:
        baseline = self.write_orchestrator(
            phase="executing",
            authorizedEpic="epic-a",
            approvedEpicIds=["epic-a"],
            approvedIssueIds=["task-a1"],
            authorizationFrozenAt="2026-03-29T01:00:00Z",
            uniqueIssuesDispatched=1,
            totalDispatches=1,
        )
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue("task-a1", parent="epic-a", state="In Progress")
        self.write_issue("epic-b", kind="epic", children=["task-b1"], state="Todo")
        self.write_issue("task-b1", parent="epic-b", state="Todo")

        stderr = io.StringIO()
        with mock.patch.object(self.freeze, "render_views", return_value=None), redirect_stderr(stderr):
            rc = self.freeze.main(["epic-b"])

        self.assertNotEqual(rc, 0)
        self.assertIn("refuse to replace", stderr.getvalue())
        self.assertEqual(self.read_json("state/orchestrator.json"), baseline)

    def test_freeze_replace_inactive_requires_completed_prior_window(self) -> None:
        baseline = self.write_orchestrator(
            phase="ready",
            approvedEpicIds=["epic-a"],
            approvedIssueIds=["task-a1"],
            authorizationFrozenAt="2026-03-29T01:00:00Z",
        )
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue("task-a1", parent="epic-a", state="Todo")
        self.write_issue("epic-b", kind="epic", children=["task-b1"], state="Todo")
        self.write_issue("task-b1", parent="epic-b", state="Todo")

        stderr = io.StringIO()
        with mock.patch.object(self.freeze, "render_views", return_value=None), redirect_stderr(stderr):
            rc = self.freeze.main(["--replace-inactive", "epic-b"])

        self.assertNotEqual(rc, 0)
        self.assertIn("current approved window is complete", stderr.getvalue())
        self.assertEqual(self.read_json("state/orchestrator.json"), baseline)

    def test_frozen_execution_fails_closed_when_approval_data_missing(self) -> None:
        self.write_orchestrator(
            phase="executing",
            authorizedEpic="epic-a",
            authorizationFrozenAt="2026-03-29T01:00:00Z",
            approvedEpicIds=[],
            approvedIssueIds=[],
        )
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue("task-a1", parent="epic-a", state="Todo")

        calls: list[list[str]] = []
        with mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)), \
                mock.patch.object(self.tick, "dispatch", side_effect=AssertionError("dispatch should not run")):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        orch = self.read_json("state/orchestrator.json")
        self.assertEqual(orch["status"], "CONTEXT_RECOVERY_BLOCKED: frozen authorization data missing or empty; dispatch disabled")
        self.assertEqual(self.read_json("state/issues/task-a1.json")["state"], "Todo")
        self.assertTrue(calls, "render helpers should still be allowed")

    def test_kill_switch_stops_tick_before_any_dispatch_work(self) -> None:
        self.stop_path.parent.mkdir(parents=True, exist_ok=True)
        self.stop_path.write_text("stop\n")
        baseline = self.write_orchestrator(
            phase="executing",
            authorizedEpic="epic-a",
            authorizationFrozenAt="2026-03-29T01:00:00Z",
            approvedEpicIds=["epic-a"],
            approvedIssueIds=["task-a1"],
        )
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue("task-a1", parent="epic-a", state="Todo")

        stdout = io.StringIO()
        with mock.patch.object(self.tick.subprocess, "run", side_effect=AssertionError("subprocess should not run")), \
                mock.patch.object(self.tick, "dispatch", side_effect=AssertionError("dispatch should not run")), \
                redirect_stdout(stdout):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        self.assertIn("STOP file present", stdout.getvalue())
        self.assertEqual(self.read_json("state/orchestrator.json"), baseline)

    def test_tick_isolated_stop_path_ignores_ambient_home_stop_file(self) -> None:
        host_home = self.root / "host-home"
        host_stop = host_home / ".openclaw" / "STOP"
        host_stop.parent.mkdir(parents=True, exist_ok=True)
        host_stop.write_text("stop\n")

        self.write_orchestrator(phase="ready", activeProject="proj", authorizedEpic="epic-a")
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue("task-a1", parent="epic-a", state="Todo", status={})

        def fake_dispatch(issue_id: str) -> int:
            self.assertEqual(issue_id, "task-a1")
            issue = self.read_json(f"state/issues/{issue_id}.json")
            issue["state"] = "In Progress"
            issue["status"] = {
                **(issue.get("status") or {}),
                "firstDispatchedAt": "2026-03-29T03:10:00Z",
                "lastDispatchedAt": "2026-03-29T03:10:00Z",
                "dispatchCount": 1,
            }
            self.write_json(f"state/issues/{issue_id}.json", issue)
            return 0

        calls: list[list[str]] = []
        with mock.patch.dict(os.environ, {"HOME": str(host_home)}), \
                mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)), \
                mock.patch.object(self.tick, "dispatch", side_effect=fake_dispatch):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        self.assertEqual(self.read_json("state/issues/task-a1.json")["state"], "In Progress")

    def test_freeze_render_failure_rolls_back_orchestrator_and_frozen_snapshot(self) -> None:
        baseline = self.write_orchestrator(
            phase="ready",
            activeProject="proj",
            authorizedEpic=None,
            status="baseline",
        )
        baseline_snapshot = self.write_frozen_authorization(
            project="proj",
            approvedEpicIds=["old-epic"],
            approvedIssueIds=["old-task"],
            authorizationFrozenAt="2026-03-29T01:00:00Z",
            authorizationProject="proj",
        )
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue("task-a1", parent="epic-a", state="Todo")

        calls: list[list[str]] = []
        stderr = io.StringIO()
        with mock.patch.object(self.freeze, "render_views", side_effect=RuntimeError("render-state.py failed")), \
                mock.patch.object(self.freeze.subprocess, "run", side_effect=self.fake_subprocess_run(calls)), \
                redirect_stderr(stderr):
            rc = self.freeze.main(["epic-a"])

        self.assertEqual(rc, 1)
        self.assertEqual(self.read_json("state/orchestrator.json"), baseline)
        self.assertEqual(self.read_json("state/frozen-authorization.json"), baseline_snapshot)
        self.assertIn("restored previous orchestrator/frozen authorization files", stderr.getvalue())

    def test_freeze_replace_inactive_render_failure_does_not_append_audit(self) -> None:
        baseline = self.write_orchestrator(
            phase="ready",
            approvedEpicIds=["epic-a"],
            approvedIssueIds=["task-a1"],
            authorizationFrozenAt="2026-03-29T01:00:00Z",
            uniqueIssuesDispatched=2,
            totalDispatches=3,
        )
        baseline_snapshot = self.read_json("state/frozen-authorization.json")
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Done")
        self.write_issue("task-a1", parent="epic-a", state="Done")
        self.write_issue("epic-b", kind="epic", children=["task-b1"], state="Todo")
        self.write_issue("task-b1", parent="epic-b", state="Todo")

        audit_path = self.root / "state" / "runs" / "freeze-authorization-audit.jsonl"
        audit_path.write_text(json.dumps({"action": "baseline"}) + "\n")
        baseline_audit = audit_path.read_text()

        calls: list[list[str]] = []
        stderr = io.StringIO()
        with mock.patch.object(self.freeze, "render_views", side_effect=RuntimeError("render-state.py failed")), \
                mock.patch.object(self.freeze.subprocess, "run", side_effect=self.fake_subprocess_run(calls)), \
                redirect_stderr(stderr):
            rc = self.freeze.main(["--replace-inactive", "epic-b"])

        self.assertEqual(rc, 1)
        self.assertEqual(self.read_json("state/orchestrator.json"), baseline)
        self.assertEqual(self.read_json("state/frozen-authorization.json"), baseline_snapshot)
        self.assertEqual(audit_path.read_text(), baseline_audit)
        self.assertIn("restored previous orchestrator/frozen authorization files", stderr.getvalue())

    def test_tick_render_failure_rolls_back_orchestrator_json(self) -> None:
        baseline = self.write_orchestrator(
            phase="executing",
            authorizedEpic="epic-a",
            authorizationFrozenAt="2026-03-29T01:00:00Z",
            approvedEpicIds=[],
            approvedIssueIds=[],
            status="before-render-failure",
        )
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue("task-a1", parent="epic-a", state="Todo")

        calls: list[list[str]] = []
        stderr = io.StringIO()
        with mock.patch.object(self.tick, "render_views", side_effect=RuntimeError("render-state.py failed")), \
                mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)), \
                redirect_stderr(stderr):
            rc = self.tick.main()

        self.assertEqual(rc, 1)
        self.assertEqual(self.read_json("state/orchestrator.json"), baseline)
        self.assertIn("restored previous orchestrator.json", stderr.getvalue())

    def test_frozen_window_does_not_rehydrate_authorization_metadata(self) -> None:
        self.write_orchestrator(
            phase="executing",
            activeProject="proj",
            authorizedEpic="epic-a",
            approvedEpicIds=["epic-a"],
            approvedIssueIds=["task-a1"],
            authorizationFrozenAt="2026-03-29T01:00:00Z",
            authorizationProject="frozen-project",
            authorizationScope="end_to_end",
            authorizationOriginEpic="frozen-root",
            authorizationGrantedAt="2026-03-28T12:00:00Z",
            authorizationSource="frozen-source",
        )
        self.write_issue(
            "epic-a",
            kind="epic",
            children=["task-a1"],
            state="Todo",
            autonomy={"granted": True, "scope": "project"},
            status={"authorizedAt": "2026-03-29T02:00:00Z", "authorizationSource": "issue-json-source"},
        )
        self.write_issue("task-a1", parent="epic-a", state="Done")

        calls: list[list[str]] = []
        with mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        orch = self.read_json("state/orchestrator.json")
        self.assertEqual(orch["authorizationProject"], "frozen-project")
        self.assertEqual(orch["authorizationScope"], "end_to_end")
        self.assertEqual(orch["authorizationOriginEpic"], "frozen-root")
        self.assertEqual(orch["authorizationGrantedAt"], "2026-03-28T12:00:00Z")
        self.assertEqual(orch["authorizationSource"], "frozen-source")
        self.assertTrue(calls)

    def test_frozen_window_without_snapshot_fails_closed(self) -> None:
        self.write_orchestrator(
            phase="executing",
            activeProject="proj",
            authorizedEpic="epic-a",
            approvedEpicIds=["epic-a"],
            approvedIssueIds=["task-a1"],
            authorizationFrozenAt="2026-03-29T01:00:00Z",
        )
        (self.root / "state" / "frozen-authorization.json").unlink()
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue("task-a1", parent="epic-a", state="Todo")

        calls: list[list[str]] = []
        with mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)), \
                mock.patch.object(self.tick, "dispatch", side_effect=AssertionError("dispatch should not run")):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        orch = self.read_json("state/orchestrator.json")
        self.assertEqual(orch["status"], "CONTEXT_RECOVERY_BLOCKED: frozen authorization snapshot missing or invalid; dispatch disabled")
        self.assertEqual(self.read_json("state/issues/task-a1.json")["state"], "Todo")

    def test_frozen_dispatch_uses_snapshot_not_live_orchestrator_state(self) -> None:
        self.write_orchestrator(
            phase="ready",
            activeProject="proj",
            authorizedEpic="epic-b",
            approvedEpicIds=["epic-b"],
            approvedIssueIds=["task-b1"],
            authorizationFrozenAt="2026-03-29T01:00:00Z",
            authorizationProject="tampered-project",
            authorizationScope="project",
            authorizationOriginEpic="epic-b",
            authorizationGrantedAt="2026-03-29T02:00:00Z",
            authorizationSource="tampered-source",
        )
        self.write_frozen_authorization(
            project="proj",
            approvedEpicIds=["epic-a"],
            approvedIssueIds=["task-a1"],
            authorizationFrozenAt="2026-03-29T01:00:00Z",
            authorizationProject="proj",
            authorizationScope="end_to_end",
            authorizationOriginEpic="epic-a",
            authorizationGrantedAt="2026-03-29T00:55:00Z",
            authorizationSource="frozen-source",
        )
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue("task-a1", parent="epic-a", state="Todo", status={})
        self.write_issue("epic-b", kind="epic", children=["task-b1"], state="Todo")
        self.write_issue("task-b1", parent="epic-b", state="Todo")

        def fake_dispatch(issue_id: str) -> int:
            self.assertEqual(issue_id, "task-a1")
            issue = self.read_json(f"state/issues/{issue_id}.json")
            issue["state"] = "In Progress"
            issue["status"] = {
                **(issue.get("status") or {}),
                "firstDispatchedAt": "2026-03-29T03:00:00Z",
                "lastDispatchedAt": "2026-03-29T03:00:00Z",
                "dispatchCount": 1,
            }
            self.write_json(f"state/issues/{issue_id}.json", issue)
            return 0

        calls: list[list[str]] = []
        with mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)), \
                mock.patch.object(self.tick, "dispatch", side_effect=fake_dispatch):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        orch = self.read_json("state/orchestrator.json")
        self.assertEqual(orch["approvedEpicIds"], ["epic-a"])
        self.assertEqual(orch["approvedIssueIds"], ["task-a1"])
        self.assertEqual(orch["authorizationProject"], "proj")
        self.assertEqual(orch["authorizationScope"], "end_to_end")
        self.assertEqual(orch["authorizationOriginEpic"], "epic-a")
        self.assertEqual(orch["authorizationGrantedAt"], "2026-03-29T00:55:00Z")
        self.assertEqual(orch["authorizationSource"], "frozen-source")
        self.assertEqual(orch["authorizedEpic"], "epic-a")
        self.assertEqual(self.read_json("state/issues/task-a1.json")["state"], "In Progress")
        self.assertEqual(self.read_json("state/issues/task-b1.json")["state"], "Blocked")

    def test_frozen_window_quarantines_unauthorized_midflight_issue(self) -> None:
        self.write_orchestrator(
            phase="executing",
            activeProject="proj",
            authorizedEpic="epic-a",
            approvedEpicIds=["epic-a"],
            approvedIssueIds=["task-a1"],
            authorizationFrozenAt="2026-03-29T01:00:00Z",
        )
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue("task-a1", parent="epic-a", state="Done")
        self.write_issue("task-extra", parent="epic-a", state="Todo")

        calls: list[list[str]] = []
        with mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        extra = self.read_json("state/issues/task-extra.json")
        self.assertEqual(extra["state"], "Blocked")
        self.assertEqual(extra["status"]["blockedReason"], "not_in_frozen_authorization_window")
        audit_lines = (self.root / "state" / "runs" / "quarantine-audit.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(audit_lines), 1)
        event = json.loads(audit_lines[0])
        self.assertEqual(event["issueId"], "task-extra")
        self.assertEqual(event["authorizationFrozenAt"], "2026-03-29T01:00:00Z")

    def test_frozen_window_leaves_unrelated_queued_backlog_from_other_project_untouched(self) -> None:
        self.write_orchestrator(
            phase="executing",
            activeProject="proj",
            authorizedEpic="epic-a",
            approvedEpicIds=["epic-a"],
            approvedIssueIds=["task-a1"],
            authorizationFrozenAt="2026-03-29T01:00:00Z",
        )
        self.write_issue("epic-a", kind="epic", project="proj", children=["task-a1"], state="Todo")
        self.write_issue("task-a1", project="proj", parent="epic-a", state="Done")
        self.write_issue("epic-b", kind="epic", project="other-proj", children=["task-extra"], state="Todo")
        self.write_issue("task-extra", project="other-proj", parent="epic-b", state="Todo")

        calls: list[list[str]] = []
        with mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        extra = self.read_json("state/issues/task-extra.json")
        self.assertEqual(extra["state"], "Todo")
        self.assertNotIn("status", extra)
        self.assertFalse((self.root / "state" / "runs" / "quarantine-audit.jsonl").exists())

    def test_frozen_window_interrupts_unauthorized_running_issue(self) -> None:
        self.write_orchestrator(
            phase="executing",
            activeProject="proj",
            authorizedEpic="epic-a",
            approvedEpicIds=["epic-a"],
            approvedIssueIds=["task-a1"],
            authorizationFrozenAt="2026-03-29T01:00:00Z",
        )
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue("task-a1", parent="epic-a", state="Done")
        self.write_issue(
            "task-extra",
            parent="epic-a",
            state="In Progress",
            status={"session": "worker-code-task-extra"},
            agents={"coder": {"sessionId": "worker-code-task-extra", "status": "running"}},
        )

        calls: list[list[str]] = []
        with mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        extra = self.read_json("state/issues/task-extra.json")
        self.assertEqual(extra["state"], "Blocked")
        self.assertEqual(extra["status"]["blockedReason"], "not_in_frozen_authorization_window")
        self.assertEqual(extra["agents"]["coder"]["status"], "interrupted")
        self.assertIn(["tmux", "kill-session", "-t", "worker-code-task-extra"], calls)

    def test_frozen_window_interrupts_running_issue_and_releases_held_locks(self) -> None:
        lock_dir = self.root / "state" / "locks" / "alpha.d"
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "holder.txt").write_text("task-extra\n")
        self.write_orchestrator(
            phase="executing",
            activeProject="proj",
            authorizedEpic="epic-a",
            approvedEpicIds=["epic-a"],
            approvedIssueIds=["task-a1"],
            authorizationFrozenAt="2026-03-29T01:00:00Z",
        )
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue("task-a1", parent="epic-a", state="Done")
        self.write_issue(
            "task-extra",
            parent="epic-a",
            state="In Progress",
            locks=["alpha"],
            status={"session": "worker-code-task-extra"},
            agents={"coder": {"sessionId": "worker-code-task-extra", "status": "running"}},
        )

        calls: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            rendered = [str(part) for part in cmd] if isinstance(cmd, (list, tuple)) else [str(cmd)]
            calls.append(rendered)
            if rendered[:2] == ["tmux", "kill-session"]:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="missing")
            if rendered[:2] == [str(self.root / "scripts" / "lock.py"), "release"]:
                shutil.rmtree(lock_dir, ignore_errors=True)
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(self.tick.subprocess, "run", side_effect=fake_run):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        extra = self.read_json("state/issues/task-extra.json")
        self.assertEqual(extra["state"], "Blocked")
        self.assertEqual(extra["status"]["blockedReason"], "not_in_frozen_authorization_window")
        self.assertFalse(lock_dir.exists())
        self.assertIn(
            [str(self.root / "scripts" / "lock.py"), "release", "alpha", "task-extra"],
            calls,
        )

    def test_frozen_window_interrupts_all_recorded_multiworker_sessions(self) -> None:
        sessions = ["worker-code-task-extra-w1", "worker-code-task-extra-w2"]
        self.write_orchestrator(
            phase="executing",
            activeProject="proj",
            authorizedEpic="epic-a",
            approvedEpicIds=["epic-a"],
            approvedIssueIds=["task-a1"],
            authorizationFrozenAt="2026-03-29T01:00:00Z",
        )
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue("task-a1", parent="epic-a", state="Done")
        self.write_issue(
            "task-extra",
            parent="epic-a",
            state="In Progress",
            status={"session": sessions[0], "sessions": sessions},
            agents={"coder": {"sessionId": sessions[0], "sessionIds": sessions, "status": "running"}},
        )

        calls: list[list[str]] = []
        with mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        extra = self.read_json("state/issues/task-extra.json")
        self.assertEqual(extra["state"], "Blocked")
        self.assertEqual(extra["status"]["blockedReason"], "not_in_frozen_authorization_window")
        self.assertEqual(extra["agents"]["coder"]["status"], "interrupted")
        self.assertIn(["tmux", "kill-session", "-t", sessions[0]], calls)
        self.assertIn(["tmux", "kill-session", "-t", sessions[1]], calls)

    def test_frozen_window_interrupts_unauthorized_running_issue_from_other_project(self) -> None:
        self.write_orchestrator(
            phase="executing",
            activeProject="proj",
            authorizedEpic="epic-a",
            approvedEpicIds=["epic-a"],
            approvedIssueIds=["task-a1"],
            authorizationFrozenAt="2026-03-29T01:00:00Z",
        )
        self.write_issue("epic-a", kind="epic", project="proj", children=["task-a1"], state="Todo")
        self.write_issue("task-a1", project="proj", parent="epic-a", state="Done")
        self.write_issue("epic-b", kind="epic", project="other-proj", children=["task-extra"], state="Todo")
        self.write_issue(
            "task-extra",
            project="other-proj",
            parent="epic-b",
            state="In Progress",
            status={"session": "worker-code-task-extra"},
            agents={"coder": {"sessionId": "worker-code-task-extra", "status": "running"}},
        )

        calls: list[list[str]] = []
        with mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        extra = self.read_json("state/issues/task-extra.json")
        self.assertEqual(extra["state"], "Blocked")
        self.assertEqual(extra["status"]["blockedReason"], "not_in_frozen_authorization_window")
        self.assertEqual(extra["agents"]["coder"]["status"], "interrupted")
        self.assertIn(["tmux", "kill-session", "-t", "worker-code-task-extra"], calls)

    def test_frozen_dispatch_updates_counters_and_audit(self) -> None:
        self.write_orchestrator(
            phase="executing",
            activeProject="proj",
            authorizedEpic="epic-a",
            approvedEpicIds=["epic-a"],
            approvedIssueIds=["task-a1"],
            authorizationFrozenAt="2026-03-29T01:00:00Z",
            uniqueIssuesDispatched=0,
            totalDispatches=0,
        )
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue("task-a1", parent="epic-a", state="Todo", status={})

        def fake_dispatch(issue_id: str) -> int:
            issue = self.read_json(f"state/issues/{issue_id}.json")
            issue["state"] = "In Progress"
            issue["status"] = {
                **(issue.get("status") or {}),
                "firstDispatchedAt": "2026-03-29T03:00:00Z",
                "lastDispatchedAt": "2026-03-29T03:00:00Z",
                "dispatchCount": 1,
            }
            self.write_json(f"state/issues/{issue_id}.json", issue)
            return 0

        calls: list[list[str]] = []
        with mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)), \
                mock.patch.object(self.tick, "dispatch", side_effect=fake_dispatch):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        orch = self.read_json("state/orchestrator.json")
        self.assertEqual(orch["uniqueIssuesDispatched"], 1)
        self.assertEqual(orch["totalDispatches"], 1)
        audit_lines = (self.root / "state" / "runs" / "dispatch-audit.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(audit_lines), 1)
        event = json.loads(audit_lines[0])
        self.assertEqual(event["issueId"], "task-a1")
        self.assertEqual(event["epicId"], "epic-a")
        self.assertEqual(event["uniqueIssues"], 1)
        self.assertEqual(event["totalDispatches"], 1)
        self.assertEqual(event["authorizationFrozenAt"], "2026-03-29T01:00:00Z")

    def test_frozen_dispatch_budget_blocks_when_unique_cap_exhausted(self) -> None:
        self.write_workflow(max_unique=1, max_total=5)
        self.write_orchestrator(
            phase="executing",
            activeProject="proj",
            authorizedEpic="epic-a",
            approvedEpicIds=["epic-a"],
            approvedIssueIds=["task-a1"],
            authorizationFrozenAt="2026-03-29T01:00:00Z",
            uniqueIssuesDispatched=1,
            totalDispatches=1,
        )
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue("task-a1", parent="epic-a", state="Todo", status={})

        calls: list[list[str]] = []
        with mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)), \
                mock.patch.object(self.tick, "dispatch", side_effect=AssertionError("dispatch should not run")):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        orch = self.read_json("state/orchestrator.json")
        self.assertEqual(orch["phase"], "blocked")
        self.assertEqual(orch["status"], "DISPATCH_BUDGET_EXHAUSTED: unique issues")
        self.assertEqual(self.read_json("state/issues/task-a1.json")["state"], "Todo")

    def test_frozen_ready_window_ignores_unapproved_authorization_sources(self) -> None:
        self.write_orchestrator(
            phase="ready",
            activeProject="proj",
            approvedEpicIds=["epic-b"],
            approvedIssueIds=["task-b1"],
            authorizationFrozenAt="2026-03-29T01:00:00Z",
            authorizationProject="proj",
            authorizationScope="end_to_end",
            authorizationOriginEpic="epic-b",
            authorizationGrantedAt="2026-03-29T00:55:00Z",
            authorizationSource="frozen-source",
        )
        self.write_issue(
            "epic-auto",
            kind="epic",
            children=["task-auto"],
            state="Todo",
            autonomy={"granted": True, "scope": "end_to_end"},
            status={"authorizedAt": "2026-03-29T02:00:00Z", "authorizationSource": "issue-json-source"},
        )
        self.write_issue("task-auto", parent="epic-auto", state="Todo")
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue("task-a1", parent="epic-a", state="Todo")
        self.write_issue("epic-b", kind="epic", children=["task-b1"], state="Todo")
        self.write_issue("task-b1", parent="epic-b", state="Todo", status={})

        def fake_dispatch(issue_id: str) -> int:
            self.assertEqual(issue_id, "task-b1")
            issue = self.read_json(f"state/issues/{issue_id}.json")
            issue["state"] = "In Progress"
            issue["status"] = {
                **(issue.get("status") or {}),
                "firstDispatchedAt": "2026-03-29T03:00:00Z",
                "lastDispatchedAt": "2026-03-29T03:00:00Z",
                "dispatchCount": 1,
            }
            self.write_json(f"state/issues/{issue_id}.json", issue)
            return 0

        calls: list[list[str]] = []
        with mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)), \
                mock.patch.object(self.tick, "dispatch", side_effect=fake_dispatch):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        orch = self.read_json("state/orchestrator.json")
        self.assertEqual(orch["authorizationProject"], "proj")
        self.assertEqual(orch["authorizationScope"], "end_to_end")
        self.assertEqual(orch["authorizationOriginEpic"], "epic-b")
        self.assertEqual(orch["authorizationGrantedAt"], "2026-03-29T00:55:00Z")
        self.assertEqual(orch["authorizationSource"], "frozen-source")
        self.assertEqual(self.read_json("state/issues/task-a1.json")["state"], "Blocked")
        self.assertEqual(self.read_json("state/issues/task-b1.json")["state"], "In Progress")

    def test_frozen_ready_window_fails_closed_with_empty_approval_data(self) -> None:
        self.write_orchestrator(
            phase="ready",
            authorizationFrozenAt="2026-03-29T01:00:00Z",
            approvedEpicIds=[],
            approvedIssueIds=[],
        )
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue("task-a1", parent="epic-a", state="Todo")

        calls: list[list[str]] = []
        with mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)), \
                mock.patch.object(self.tick, "dispatch", side_effect=AssertionError("dispatch should not run")):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        orch = self.read_json("state/orchestrator.json")
        self.assertEqual(orch["status"], "CONTEXT_RECOVERY_BLOCKED: frozen authorization data missing or empty; dispatch disabled")
        self.assertEqual(self.read_json("state/issues/task-a1.json")["state"], "Todo")

    def test_frozen_ready_budget_blocks_before_dispatch(self) -> None:
        self.write_workflow(max_unique=1, max_total=1)
        self.write_orchestrator(
            phase="ready",
            activeProject="proj",
            approvedEpicIds=["epic-a"],
            approvedIssueIds=["task-a1"],
            authorizationFrozenAt="2026-03-29T01:00:00Z",
            uniqueIssuesDispatched=1,
            totalDispatches=1,
        )
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue("task-a1", parent="epic-a", state="Todo", status={})

        calls: list[list[str]] = []
        with mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)), \
                mock.patch.object(self.tick, "dispatch", side_effect=AssertionError("dispatch should not run")):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        orch = self.read_json("state/orchestrator.json")
        self.assertEqual(orch["phase"], "blocked")
        self.assertEqual(orch["status"], "DISPATCH_BUDGET_EXHAUSTED: total dispatches")
        self.assertEqual(self.read_json("state/issues/task-a1.json")["state"], "Todo")

    def test_frozen_continuation_ignores_parent_only_unauthorized_task(self) -> None:
        self.write_orchestrator(
            phase="executing",
            activeProject="proj",
            authorizedEpic="epic-a",
            approvedEpicIds=["epic-a", "epic-b"],
            approvedIssueIds=["task-a1", "task-b1"],
            authorizationFrozenAt="2026-03-29T01:00:00Z",
        )
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue("task-a1", parent="epic-a", state="Done")
        self.write_issue("task-parent-only", parent="epic-a", state="Todo")
        self.write_issue("epic-b", kind="epic", children=["task-b1"], state="Todo")
        self.write_issue("task-b1", parent="epic-b", state="Todo", status={})

        def fake_dispatch(issue_id: str) -> int:
            self.assertEqual(issue_id, "task-b1")
            issue = self.read_json(f"state/issues/{issue_id}.json")
            issue["state"] = "In Progress"
            issue["status"] = {
                **(issue.get("status") or {}),
                "firstDispatchedAt": "2026-03-29T03:05:00Z",
                "lastDispatchedAt": "2026-03-29T03:05:00Z",
                "dispatchCount": 1,
            }
            self.write_json(f"state/issues/{issue_id}.json", issue)
            return 0

        calls: list[list[str]] = []
        with mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)), \
                mock.patch.object(self.tick, "dispatch", side_effect=fake_dispatch):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        self.assertEqual(self.read_json("state/issues/task-parent-only.json")["state"], "Blocked")
        orch = self.read_json("state/orchestrator.json")
        self.assertEqual(orch["authorizedEpic"], "epic-b")
        self.assertEqual(self.read_json("state/issues/task-b1.json")["state"], "In Progress")

    def test_unfrozen_blanket_go_legacy_continuation_still_works_without_approved_ids(self) -> None:
        self.write_orchestrator(
            phase="ready",
            activeProject="proj",
            authorizedEpic=None,
            approvedEpicIds=[],
            approvedIssueIds=[],
            authorizationFrozenAt=None,
        )
        self.write_issue(
            "epic-zeta",
            kind="epic",
            children=["task-zeta"],
            state="Todo",
            autonomy={"granted": True, "scope": "end_to_end"},
            status={"authorizedAt": "2026-03-29T02:00:00Z", "authorizationSource": "legacy blanket-go"},
        )
        self.write_issue("task-zeta", parent="epic-zeta", state="Todo", status={})
        self.write_issue("epic-alpha", kind="epic", children=["task-alpha"], state="Todo")
        self.write_issue("task-alpha", parent="epic-alpha", state="Todo", status={})

        def fake_dispatch(issue_id: str) -> int:
            self.assertEqual(issue_id, "task-alpha")
            issue = self.read_json(f"state/issues/{issue_id}.json")
            issue["state"] = "In Progress"
            issue["status"] = {
                **(issue.get("status") or {}),
                "firstDispatchedAt": "2026-03-29T03:10:00Z",
                "lastDispatchedAt": "2026-03-29T03:10:00Z",
                "dispatchCount": 1,
            }
            self.write_json(f"state/issues/{issue_id}.json", issue)
            return 0

        calls: list[list[str]] = []
        with mock.patch.object(self.tick, "hydrate_project_authorization", wraps=self.tick.hydrate_project_authorization) as hydrate_spy, \
                mock.patch.object(self.tick, "find_next_eligible_epic", wraps=self.tick.find_next_eligible_epic) as find_spy, \
                mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)), \
                mock.patch.object(self.tick, "dispatch", side_effect=fake_dispatch):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        self.assertEqual(hydrate_spy.call_count, 1)
        self.assertGreaterEqual(find_spy.call_count, 1)
        self.assertTrue(all(call.kwargs.get("allowed_ids") is None for call in find_spy.call_args_list))

        orch = self.read_json("state/orchestrator.json")
        self.assertIsNone(orch["authorizationFrozenAt"])
        self.assertEqual(orch["approvedEpicIds"], [])
        self.assertEqual(orch["approvedIssueIds"], [])
        self.assertEqual(orch["authorizationOriginEpic"], "epic-zeta")
        self.assertEqual(orch["authorizationSource"], "legacy blanket-go")
        self.assertEqual(orch["authorizedEpic"], "epic-alpha")
        self.assertEqual(self.read_json("state/issues/task-alpha.json")["state"], "In Progress")
        self.assertEqual(self.read_json("state/issues/task-zeta.json")["state"], "Todo")

    def test_partial_lock_acquire_rolls_back_acquired_locks(self) -> None:
        self.write_orchestrator(phase="ready", activeProject="proj", authorizedEpic="epic-a")
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue("task-a1", parent="epic-a", state="Todo", locks=["alpha", "beta"], status={})

        calls: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            rendered = [str(part) for part in cmd] if isinstance(cmd, (list, tuple)) else [str(cmd)]
            calls.append(rendered)
            if rendered[:2] == [str(self.root / "scripts" / "lock.py"), "acquire"] and rendered[2] == "alpha":
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            if rendered[:2] == [str(self.root / "scripts" / "lock.py"), "acquire"] and rendered[2] == "beta":
                return types.SimpleNamespace(returncode=1, stdout="", stderr="busy")
            if rendered[:2] == [str(self.root / "scripts" / "lock.py"), "release"]:
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(self.tick.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(self.tick, "dispatch", side_effect=AssertionError("dispatch should not run")):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        self.assertEqual(self.read_json("state/issues/task-a1.json")["state"], "Todo")
        self.assertIn(
            [str(self.root / "scripts" / "lock.py"), "release", "alpha", "task-a1"],
            calls,
        )

    def test_failed_dispatch_releases_acquired_locks(self) -> None:
        self.write_orchestrator(phase="ready", activeProject="proj", authorizedEpic="epic-a")
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue("task-a1", parent="epic-a", state="Todo", locks=["alpha"], status={})

        calls: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            rendered = [str(part) for part in cmd] if isinstance(cmd, (list, tuple)) else [str(cmd)]
            calls.append(rendered)
            if rendered[:2] == [str(self.root / "scripts" / "lock.py"), "acquire"]:
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            if rendered[:2] == [str(self.root / "scripts" / "lock.py"), "release"]:
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(self.tick.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(self.tick, "dispatch", return_value=1):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        self.assertEqual(self.read_json("state/issues/task-a1.json")["state"], "Todo")
        self.assertIn(
            [str(self.root / "scripts" / "lock.py"), "release", "alpha", "task-a1"],
            calls,
        )

    def test_single_worker_quarantined_issue_dispatches_in_phase_1(self) -> None:
        self.write_orchestrator(phase="ready", activeProject="proj", authorizedEpic="epic-a")
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue(
            "task-a1",
            parent="epic-a",
            state="Todo",
            quarantine=True,
            workspaceIsolation="isolated",
            allowedPaths=["/tmp"],
            locks=["alpha"],
            status={},
        )

        def fake_dispatch(issue_id: str) -> int:
            self.assertEqual(issue_id, "task-a1")
            issue = self.read_json(f"state/issues/{issue_id}.json")
            issue["state"] = "In Progress"
            issue["status"] = {
                **(issue.get("status") or {}),
                "firstDispatchedAt": "2026-04-14T00:00:00Z",
                "lastDispatchedAt": "2026-04-14T00:00:00Z",
                "dispatchCount": 1,
            }
            self.write_json(f"state/issues/{issue_id}.json", issue)
            return 0

        calls: list[list[str]] = []
        with mock.patch.object(self.tick.os, "name", "posix"), \
                mock.patch.object(self.tick.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"), \
                mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)), \
                mock.patch.object(self.tick, "dispatch_multiworker", side_effect=AssertionError("single-worker quarantine should use normal dispatch")), \
                mock.patch.object(self.tick, "dispatch", side_effect=fake_dispatch):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        issue = self.read_json("state/issues/task-a1.json")
        self.assertEqual(issue["state"], "In Progress")
        self.assertNotIn("blockedReason", issue.get("status") or {})
        self.assertIn([str(self.root / "scripts" / "lock.py"), "acquire", "alpha", "task-a1"], calls)

    def test_quarantined_multiworker_issue_remains_blocked_in_phase_1(self) -> None:
        self.write_workflow(multi_worker_enabled=True)
        self.write_orchestrator(phase="ready", activeProject="proj", authorizedEpic="epic-a")
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue(
            "task-a1",
            parent="epic-a",
            state="Todo",
            quarantine=True,
            workspaceIsolation="isolated",
            allowedPaths=["/tmp"],
            locks=["alpha"],
            concurrencyOverride=2,
            status={},
        )

        calls: list[list[str]] = []
        with mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)), \
                mock.patch.object(self.tick, "dispatch_multiworker", side_effect=AssertionError("quarantine multi-worker should stay blocked in phase 1")), \
                mock.patch.object(self.tick, "dispatch", side_effect=AssertionError("quarantine multi-worker should stay blocked in phase 1")):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        issue = self.read_json("state/issues/task-a1.json")
        self.assertEqual(issue["state"], "Blocked")
        self.assertTrue(issue["status"]["blocked"])
        self.assertIn("blockedAt", issue["status"])
        self.assertEqual(issue["status"]["blockedReason"], "quarantine multi-worker remains disabled in phase 1")

    def test_quarantined_issue_blocks_workspace_state_allowed_path(self) -> None:
        self.write_orchestrator(phase="ready", activeProject="proj", authorizedEpic="epic-a")
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue(
            "task-a1",
            parent="epic-a",
            state="Todo",
            quarantine=True,
            workspaceIsolation="isolated",
            allowedPaths=[str(self.root / "state" / "**")],
            locks=["alpha"],
            status={},
        )

        calls: list[list[str]] = []
        with mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)), \
                mock.patch.object(self.tick, "dispatch", side_effect=AssertionError("invalid quarantine allowedPaths should block before dispatch")), \
                mock.patch.object(self.tick, "dispatch_multiworker", side_effect=AssertionError("invalid quarantine allowedPaths should block before dispatch")):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        issue = self.read_json("state/issues/task-a1.json")
        self.assertEqual(issue["state"], "Blocked")
        self.assertIn("workspace control-plane path", issue["status"]["blockedReason"])

    def test_quarantined_issue_blocks_workspace_scripts_allowed_path(self) -> None:
        self.write_orchestrator(phase="ready", activeProject="proj", authorizedEpic="epic-a")
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue(
            "task-a1",
            parent="epic-a",
            state="Todo",
            quarantine=True,
            workspaceIsolation="isolated",
            allowedPaths=[str(self.root / "scripts" / "**")],
            locks=["alpha"],
            status={},
        )

        calls: list[list[str]] = []
        with mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)), \
                mock.patch.object(self.tick, "dispatch", side_effect=AssertionError("invalid quarantine allowedPaths should block before dispatch")), \
                mock.patch.object(self.tick, "dispatch_multiworker", side_effect=AssertionError("invalid quarantine allowedPaths should block before dispatch")):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        issue = self.read_json("state/issues/task-a1.json")
        self.assertEqual(issue["state"], "Blocked")
        self.assertIn("workspace control-plane path", issue["status"]["blockedReason"])

    def test_quarantined_issue_blocks_workspace_root_allowed_path(self) -> None:
        self.write_orchestrator(phase="ready", activeProject="proj", authorizedEpic="epic-a")
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue(
            "task-a1",
            parent="epic-a",
            state="Todo",
            quarantine=True,
            workspaceIsolation="isolated",
            allowedPaths=[str(self.root)],
            locks=["alpha"],
            status={},
        )

        calls: list[list[str]] = []
        with mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)), \
                mock.patch.object(self.tick, "dispatch", side_effect=AssertionError("invalid quarantine allowedPaths should block before dispatch")), \
                mock.patch.object(self.tick, "dispatch_multiworker", side_effect=AssertionError("invalid quarantine allowedPaths should block before dispatch")):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        issue = self.read_json("state/issues/task-a1.json")
        self.assertEqual(issue["state"], "Blocked")
        self.assertIn("reopen workspace root", issue["status"]["blockedReason"])

    def test_quarantined_issue_blocks_workspace_root_control_file_allowed_path(self) -> None:
        self.write_orchestrator(phase="ready", activeProject="proj", authorizedEpic="epic-a")
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue(
            "task-a1",
            parent="epic-a",
            state="Todo",
            quarantine=True,
            workspaceIsolation="isolated",
            allowedPaths=[str(self.root / "CODER.md")],
            locks=["alpha"],
            status={},
        )

        calls: list[list[str]] = []
        with mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)), \
                mock.patch.object(self.tick, "dispatch", side_effect=AssertionError("invalid quarantine allowedPaths should block before dispatch")), \
                mock.patch.object(self.tick, "dispatch_multiworker", side_effect=AssertionError("invalid quarantine allowedPaths should block before dispatch")):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        issue = self.read_json("state/issues/task-a1.json")
        self.assertEqual(issue["state"], "Blocked")
        self.assertIn("workspace control-plane path", issue["status"]["blockedReason"])

    def test_quarantined_issue_blocks_missing_project_root(self) -> None:
        self.write_orchestrator(phase="ready", activeProject="proj", authorizedEpic="epic-a")
        self.write_issue("epic-a", kind="epic", children=["task-a1"], state="Todo")
        self.write_issue(
            "task-a1",
            parent="epic-a",
            project="demo-project",
            state="Todo",
            quarantine=True,
            workspaceIsolation="isolated",
            allowedPaths=["/tmp"],
            locks=["alpha"],
            status={},
        )

        calls: list[list[str]] = []
        with mock.patch.object(self.tick.subprocess, "run", side_effect=self.fake_subprocess_run(calls)), \
                mock.patch.object(self.tick, "dispatch", side_effect=AssertionError("missing project root should block before dispatch")), \
                mock.patch.object(self.tick, "dispatch_multiworker", side_effect=AssertionError("missing project root should block before dispatch")):
            rc = self.tick.main()

        self.assertEqual(rc, 0)
        issue = self.read_json("state/issues/task-a1.json")
        self.assertEqual(issue["state"], "Blocked")
        self.assertIn("quarantine project root missing", issue["status"]["blockedReason"])

class RuntimeReviewFailClosedScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.home = self.root / "home"
        self.workspace = self.home / ".openclaw" / "workspace"
        self.scripts_dir = self.workspace / "scripts"
        self.debates_dir = self.workspace / "debates"
        self.issues_dir = self.workspace / "state" / "issues"
        self.worktree = self.root / "worktree"
        self.bin_dir = self.root / "bin"
        for directory in (self.scripts_dir, self.debates_dir, self.issues_dir, self.worktree, self.bin_dir):
            directory.mkdir(parents=True, exist_ok=True)
        for script_name in (
            "audit_output_validator.py",
            "review-dispatch.sh",
            "run-auditor.sh",
            "run-mediator.sh",
            "dispatch-issue.sh",
            "list-bound-execution-gate-check.sh",
            "openclaw-paths.sh",
            "openclaw-harness-worktree-guard.sh",
        ):
            shutil.copy2(REPO_ROOT / "scripts" / script_name, self.scripts_dir / script_name)
        shutil.copytree(
            REPO_ROOT / "scripts" / "list-bound-execution-gate",
            self.scripts_dir / "list-bound-execution-gate",
        )
        for role_file in (
            "AUDITOR_ALPHA.md",
            "AUDITOR_ALPHA_PRIME.md",
            "AUDITOR_BETA.md",
            "MEDIATOR.md",
            "REVIEWER_DATA.md",
            "REVIEWER_OPS.md",
            "CODER.md",
            "ANALYST.md",
            "RESEARCHER.md",
            "PIPELINER.md",
        ):
            (self.workspace / role_file).write_text(f"# {role_file}\nUse only read-only evidence.\n", encoding="utf-8")
        self.responses_path = self.root / "openclaw-responses.json"
        self.fake_log_path = self.root / "openclaw-calls.jsonl"
        self.fake_sandbox_log_path = self.root / "openclaw-sandbox-calls.jsonl"
        self.fake_index_path = self.root / "openclaw-index.txt"
        self.fake_sandbox_explain: object | None = None
        self.fake_sandbox_explain_raw: str | None = None
        self.fake_sandbox_explain_returncode = 0
        self.install_fake_openclaw()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def env(self) -> dict[str, str]:
        env = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}",
            "OPENCLAW_WORKSPACE_ROOT": str(self.workspace),
            "OPENCLAW_FAKE_RESPONSES": str(self.responses_path),
            "OPENCLAW_FAKE_LOG": str(self.fake_log_path),
            "OPENCLAW_FAKE_SANDBOX_LOG": str(self.fake_sandbox_log_path),
            "OPENCLAW_FAKE_INDEX": str(self.fake_index_path),
        }
        if self.fake_sandbox_explain_returncode:
            env["OPENCLAW_FAKE_SANDBOX_EXPLAIN_RC"] = str(self.fake_sandbox_explain_returncode)
        if self.fake_sandbox_explain_raw is not None:
            env["OPENCLAW_FAKE_SANDBOX_EXPLAIN_RAW"] = self.fake_sandbox_explain_raw
        elif self.fake_sandbox_explain is not None:
            env["OPENCLAW_FAKE_SANDBOX_EXPLAIN"] = json.dumps(self.fake_sandbox_explain)
        return env

    def install_fake_openclaw(self) -> None:
        fake = self.bin_dir / "openclaw"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, re, sys\n"
            "if sys.argv[1:3] == ['sandbox', 'explain']:\n"
            "    sandbox_log = pathlib.Path(os.environ.get('OPENCLAW_FAKE_SANDBOX_LOG', os.devnull))\n"
            "    if str(sandbox_log) != os.devnull:\n"
            "        sandbox_log.parent.mkdir(parents=True, exist_ok=True)\n"
            "        with sandbox_log.open('a', encoding='utf-8') as fh:\n"
            "            fh.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
            "    rc = int(os.environ.get('OPENCLAW_FAKE_SANDBOX_EXPLAIN_RC', '0'))\n"
            "    if rc:\n"
            "        sys.stderr.write('simulated sandbox explain failure\\n')\n"
            "        raise SystemExit(rc)\n"
            "    default = {'sandbox': {'tools': {'allow': ['read'], 'deny': ['exec', 'process', 'write', 'edit', 'apply_patch', 'gateway', 'cron', 'nodes', 'browser', 'canvas', 'sessions_send', 'sessions_spawn', 'subagents']}}}\n"
            "    if 'OPENCLAW_FAKE_SANDBOX_EXPLAIN_RAW' in os.environ:\n"
            "        payload = os.environ['OPENCLAW_FAKE_SANDBOX_EXPLAIN_RAW']\n"
            "    else:\n"
            "        payload = os.environ.get('OPENCLAW_FAKE_SANDBOX_EXPLAIN')\n"
            "    sys.stdout.write(payload if payload is not None else json.dumps(default))\n"
            "    raise SystemExit(0)\n"
            "responses_path = pathlib.Path(os.environ['OPENCLAW_FAKE_RESPONSES'])\n"
            "log_path = pathlib.Path(os.environ['OPENCLAW_FAKE_LOG'])\n"
            "idx_path = pathlib.Path(os.environ['OPENCLAW_FAKE_INDEX'])\n"
            "responses = json.loads(responses_path.read_text()) if responses_path.exists() else []\n"
            "idx = int(idx_path.read_text()) if idx_path.exists() else 0\n"
            "resp = responses[idx] if idx < len(responses) else (responses[-1] if responses else {})\n"
            "idx_path.write_text(str(idx + 1))\n"
            "log_path.parent.mkdir(parents=True, exist_ok=True)\n"
            "with log_path.open('a', encoding='utf-8') as fh:\n"
            "    fh.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
            "message = ''\n"
            "if '--message' in sys.argv:\n"
            "    try:\n"
            "        message = sys.argv[sys.argv.index('--message') + 1]\n"
            "    except Exception:\n"
            "        message = ''\n"
            "match = re.search(r'^Task ID:\\s*(\\S+)', message, re.M)\n"
            "task_id = match.group(1) if match else 'task-1'\n"
            "profile_match = re.search(r'reviewProfile=\"?([A-Za-z0-9_]+)\"?', message) or re.search(r'^Review profile:\\s*(\\S+)', message, re.M)\n"
            "tier_match = re.search(r'reviewTier=\"?([A-Za-z0-9_]+)\"?', message) or re.search(r'^Review tier:\\s*(\\S+)', message, re.M)\n"
            "role_match = re.search(r'role=\"?([^\"\\s]+)\"?', message) or re.search(r'^Review role label:\\s*(\\S+)', message, re.M) or re.search(r'according to your role \\(([^)]+)\\)', message)\n"
            "profile = profile_match.group(1) if profile_match else 'security_code'\n"
            "tier = tier_match.group(1) if tier_match else 'primary_checker'\n"
            "role = role_match.group(1) if role_match else 'alpha'\n"
            "stdout = resp.get('stdout', '').replace('SUBJECT_PLACEHOLDER', task_id)\n"
            "stdout = stdout.replace('PROFILE_PLACEHOLDER', profile).replace('TIER_PLACEHOLDER', tier).replace('ROLE_PLACEHOLDER', role)\n"
            "sys.stdout.write(stdout)\n"
            "sys.stderr.write(resp.get('stderr', ''))\n"
            "raise SystemExit(int(resp.get('returncode', 0)))\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)

    def set_openclaw_responses(self, *responses: dict[str, object]) -> None:
        self.responses_path.write_text(json.dumps(list(responses), indent=2) + "\n", encoding="utf-8")
        self.fake_index_path.write_text("0", encoding="utf-8")
        self.fake_log_path.write_text("", encoding="utf-8")
        self.fake_sandbox_log_path.write_text("", encoding="utf-8")

    def openclaw_calls(self) -> list[dict[str, object]]:
        if not self.fake_log_path.exists() or not self.fake_log_path.read_text().strip():
            return []
        return [json.loads(line) for line in self.fake_log_path.read_text().splitlines() if line.strip()]

    def openclaw_sandbox_calls(self) -> list[dict[str, object]]:
        if not self.fake_sandbox_log_path.exists() or not self.fake_sandbox_log_path.read_text().strip():
            return []
        return [json.loads(line) for line in self.fake_sandbox_log_path.read_text().splitlines() if line.strip()]

    def assert_final_verdict_complete(
        self,
        final_verdict: dict[str, object],
        *,
        review_profile: str,
        review_tier: str,
    ) -> None:
        for key in (
            "requiredRoles",
            "completedRoles",
            "reviewerArtifactRefs",
            "validationStatus",
            "requestedClaim",
            "proofTier",
            "subject",
            "subjectRef",
            "subjectCommit",
            "reviewProfile",
            "reviewTier",
            "reviewerIdentity",
        ):
            self.assertIn(key, final_verdict)
        self.assertIsInstance(final_verdict["requiredRoles"], list)
        self.assertIsInstance(final_verdict["completedRoles"], list)
        self.assertIsInstance(final_verdict["reviewerArtifactRefs"], list)
        self.assertTrue(final_verdict["reviewerArtifactRefs"])
        self.assertEqual(final_verdict["reviewProfile"], review_profile)
        self.assertEqual(final_verdict["reviewTier"], review_tier)
        self.assertIsInstance(final_verdict["subjectRef"], dict)
        subject_ref = final_verdict["subjectRef"]
        assert isinstance(subject_ref, dict)
        self.assertIn("commit", subject_ref)
        self.assertIsInstance(final_verdict["reviewerIdentity"], dict)
        reviewer_identity = final_verdict["reviewerIdentity"]
        assert isinstance(reviewer_identity, dict)
        self.assertEqual(reviewer_identity["reviewProfile"], review_profile)
        self.assertEqual(reviewer_identity["reviewTier"], review_tier)
        self.assertIn("reviewRoute", final_verdict)
        review_route = final_verdict["reviewRoute"]
        self.assertIsInstance(review_route, dict)
        assert isinstance(review_route, dict)
        self.assertEqual(review_route["reviewProfile"], review_profile)
        self.assertEqual(review_route["reviewTier"], review_tier)
        self.assertIn("requiredRoles", review_route)
        self.assertIn("completedRoles", review_route)

    def run_workspace_script(self, script_name: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.scripts_dir / script_name), *args],
            cwd=str(REPO_ROOT),
            env=self.env(),
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )

    def write_issue(self, issue_id: str, **overrides: object) -> None:
        issue = {
            "id": issue_id,
            "project": "proj",
            "title": issue_id,
            "state": "Todo",
            "workerMode": "code",
            "branch": "feature/test",
            "workspace": str(self.worktree),
        }
        issue.update(overrides)
        (self.issues_dir / f"{issue_id}.json").write_text(json.dumps(issue, indent=2) + "\n", encoding="utf-8")

    def authorize_dispatch_issue(self, issue_id: str, *, epic_id: str = "epic-dispatch") -> None:
        issue_path = self.issues_dir / f"{issue_id}.json"
        issue = json.loads(issue_path.read_text(encoding="utf-8"))
        issue["parent"] = epic_id
        issue_path.write_text(json.dumps(issue, indent=2) + "\n", encoding="utf-8")
        (self.workspace / "state" / "orchestrator.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "activeProject": "proj",
                    "phase": "executing",
                    "authorizedEpic": epic_id,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (self.issues_dir / f"{epic_id}.json").write_text(
            json.dumps(
                {
                    "id": epic_id,
                    "project": "proj",
                    "title": "Authorized dispatch fixture",
                    "state": "Todo",
                    "kind": "epic",
                    "children": [issue_id],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def valid_auditor_output(
        self,
        verdict: str = "APPROVE",
        task_id: str = "SUBJECT_PLACEHOLDER",
        *,
        branch: str = "feature/test",
        worktree: str | None = None,
        review_profile: str = "PROFILE_PLACEHOLDER",
        review_tier: str = "TIER_PLACEHOLDER",
        role: str = "ROLE_PLACEHOLDER",
    ) -> str:
        payload = {
            "subject": task_id,
            "requestedClaim": f"review the bounded task handoff for {task_id}",
            "proofTier": "candidate",
            "subjectRef": {
                "branch": branch,
                "worktree": worktree if worktree is not None else str(self.worktree),
                "commit": "abc123",
            },
            "reviewRoute": {
                "reviewProfile": review_profile,
                "reviewTier": review_tier,
                "role": role,
            },
            "verdict": verdict,
            "findings": [],
            "confidence": "HIGH",
            "no_findings_reason": "Reviewed the requested scope with concrete evidence references.",
            "evidence": ["scripts/example.py:1-2 reviewed for the test fixture"],
        }
        return (
            "## Findings\nNone.\n\n"
            "## Non-findings\n- scripts/example.py:1-2 was reviewed in this temp fixture.\n\n"
            "## Recommended fixes\nNone.\n\n"
            "## Summary\nStructured approval fixture.\n\n"
            "```json\n"
            f"{json.dumps(payload, indent=2)}\n"
            "```\n"
        )

    def raw_source_dump(self) -> str:
        lines = ["#!/usr/bin/env python3", "def leaked_source():"]
        lines.extend(f"    value_{i} = {i};" for i in range(40))
        return "\n".join(lines) + "\n"

    def valid_mediator_output(self, task_id: str, verdict: str = "ACCEPT") -> str:
        return (
            f"MEDIATOR_VERDICT: {verdict}\n"
            f"Artifact path: debates/{task_id}/mediator-ruling.md\n\n"
            f"Subject: {task_id}\n"
            f"Requested claim: mediated review for {task_id}\n"
            "Proof tier: candidate\n"
            "Branch: feature/test\n"
            f"Worktree: {self.worktree}\n"
            "Review route: primary_checker_mediator\n\n"
            "## Evidence\n"
            f"- debates/{task_id}/reviewer-data-primary.md:1-20\n"
            f"- debates/{task_id}/reviewer-data-checker.md:1-20\n\n"
            "## Decision\n"
            "Mediated fixture accepts the supplied evidence.\n"
        )

    def write_valid_mediator_debate(self, task_id: str) -> Path:
        self.write_issue(task_id)
        debate_dir = self.debates_dir / task_id
        debate_dir.mkdir(parents=True, exist_ok=True)
        self.write_security_auditor_artifacts(debate_dir, task_id)
        (debate_dir / "debate-log.md").write_text("# Debate\n", encoding="utf-8")
        return debate_dir

    def write_security_auditor_artifacts(self, debate_dir: Path, task_id: str) -> None:
        for name, role in (
            ("alpha-initial.md", "alpha"),
            ("alpha-prime-initial.md", "alpha-prime"),
            ("beta-initial.md", "beta"),
        ):
            (debate_dir / name).write_text(
                self.valid_auditor_output(
                    task_id=task_id,
                    review_profile="security_code",
                    review_tier="primary_checker_mediator",
                    role=role,
                ),
                encoding="utf-8",
            )

    def test_review_dispatch_invalid_generic_reviewer_output_never_approves(self) -> None:
        scenarios = (
            ("raw_dump", {"stdout": self.raw_source_dump()}, "raw_source_dump"),
            ("agent_failure", {"stderr": "simulated failure", "returncode": 33}, "agent_command_failed"),
        )
        for name, response, expected_reason in scenarios:
            with self.subTest(name=name):
                task_id = f"case-generic-reviewer-{name}"
                self.write_issue(task_id, reviewProfile="data_validation", reviewTier="single_worker")
                self.set_openclaw_responses(response)

                result = self.run_workspace_script("review-dispatch.sh", task_id)

                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                debate_dir = self.debates_dir / task_id
                final_verdict = json.loads((debate_dir / "final-verdict.json").read_text(encoding="utf-8"))
                self.assertEqual(final_verdict["verdict"], "REQUEST_CHANGES")
                validation = json.loads((debate_dir / "reviewer-data-validation.json").read_text(encoding="utf-8"))
                self.assertEqual(validation["status"], "invalid_output")
                self.assertEqual(validation["action"], "fail_closed")
                self.assertIn(expected_reason, ";".join(validation.get("reasons", [])))

    def test_review_dispatch_unsafe_generic_reviewer_tool_policy_never_calls_agent(self) -> None:
        task_id = "case-generic-reviewer-unsafe-policy"
        self.write_issue(task_id, reviewProfile="data_validation", reviewTier="single_worker")
        self.fake_sandbox_explain = {"sandbox": {"tools": {"allow": ["read", "exec", "write"], "deny": []}}}
        self.set_openclaw_responses({"stdout": self.valid_auditor_output("APPROVE")})

        result = self.run_workspace_script("review-dispatch.sh", task_id)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        debate_dir = self.debates_dir / task_id
        final_verdict = json.loads((debate_dir / "final-verdict.json").read_text(encoding="utf-8"))
        self.assertEqual(final_verdict["verdict"], "REQUEST_CHANGES")
        validation = json.loads((debate_dir / "reviewer-data-validation.json").read_text(encoding="utf-8"))
        self.assertEqual(validation["status"], "invalid_output")
        self.assertEqual(validation["action"], "fail_closed")
        self.assertIn("unsafe_reviewer_tool_policy", ";".join(validation.get("reasons", [])))
        self.assertEqual(self.openclaw_calls(), [])

    def test_review_dispatch_final_verdict_escapes_quoted_subject_refs(self) -> None:
        task_id = "case-generic-reviewer-quoted-path"
        quoted_worktree = self.root / 'worktree "quoted"'
        quoted_worktree.mkdir(parents=True, exist_ok=True)
        quoted_branch = 'feature/"quoted"'
        self.write_issue(
            task_id,
            reviewProfile="data_validation",
            reviewTier="single_worker",
            branch=quoted_branch,
            workspace=str(quoted_worktree),
        )
        self.set_openclaw_responses(
            {
                "stdout": self.valid_auditor_output(
                    "APPROVE",
                    branch=quoted_branch,
                    worktree=str(quoted_worktree),
                )
            }
        )

        result = self.run_workspace_script("review-dispatch.sh", task_id)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        debate_dir = self.debates_dir / task_id
        final_verdict = json.loads((debate_dir / "final-verdict.json").read_text(encoding="utf-8"))
        self.assertEqual(final_verdict["verdict"], "APPROVE")
        self.assertEqual(final_verdict["subjectRef"]["branch"], quoted_branch)
        self.assertEqual(final_verdict["subjectRef"]["worktree"], str(quoted_worktree))
        self.assert_final_verdict_complete(final_verdict, review_profile="data_validation", review_tier="single_worker")

    def test_review_scripts_do_not_use_derived_active_tasks_for_route_metadata(self) -> None:
        stale_worktree = self.root / "stale-active-task-worktree"
        stale_worktree.mkdir(parents=True, exist_ok=True)
        state_dir = self.workspace / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "active-tasks.json").write_text(
            json.dumps(
                {
                    "_meta": {"derived": True, "authoritative": False},
                    "tasks": [
                        {
                            "id": "case-no-derived-fallback",
                            "branch": "stale-derived-branch",
                            "workspace": str(stale_worktree),
                            "agents": {"coder": {"branch": "stale-derived-branch", "worktree": str(stale_worktree)}},
                        }
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        task_id = "case-no-derived-fallback"
        self.write_issue(task_id, reviewProfile="data_validation", reviewTier="single_worker", branch="", workspace="")
        self.set_openclaw_responses({"stdout": self.valid_auditor_output("APPROVE", task_id=task_id)})

        review = self.run_workspace_script("review-dispatch.sh", task_id)

        self.assertEqual(review.returncode, 0, review.stderr + review.stdout)
        final_verdict = json.loads((self.debates_dir / task_id / "final-verdict.json").read_text(encoding="utf-8"))
        self.assertEqual(final_verdict["subjectRef"]["branch"], "")
        self.assertEqual(final_verdict["subjectRef"]["worktree"], "")
        self.assertIn("No task branch", final_verdict["subjectRef"]["branchNotApplicableReason"])
        self.assertIn("No task worktree", final_verdict["subjectRef"]["worktreeNotApplicableReason"])

        auditor = self.run_workspace_script("run-auditor.sh", task_id, "alpha")
        self.assertNotEqual(auditor.returncode, 0)
        self.assertIn("No branch found", auditor.stderr + auditor.stdout)

        debate_dir = self.debates_dir / task_id
        debate_dir.mkdir(parents=True, exist_ok=True)
        (debate_dir / "debate-log.md").write_text("# Debate\n", encoding="utf-8")
        mediator = self.run_workspace_script("run-mediator.sh", task_id)
        self.assertNotEqual(mediator.returncode, 0)
        self.assertIn("No worktree/workspace", mediator.stderr + mediator.stdout)

    def test_strict_reviewer_policy_missing_allow_array_blocks(self) -> None:
        task_id = "case-generic-reviewer-missing-allow"
        self.write_issue(task_id, reviewProfile="data_validation", reviewTier="single_worker")
        self.fake_sandbox_explain = {"sandbox": {"tools": {"deny": []}}}
        self.set_openclaw_responses({"stdout": self.valid_auditor_output("APPROVE")})

        result = self.run_workspace_script("review-dispatch.sh", task_id)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        debate_dir = self.debates_dir / task_id
        final_verdict = json.loads((debate_dir / "final-verdict.json").read_text(encoding="utf-8"))
        self.assertEqual(final_verdict["verdict"], "REQUEST_CHANGES")
        validation = json.loads((debate_dir / "reviewer-data-validation.json").read_text(encoding="utf-8"))
        self.assertEqual(validation["status"], "invalid_output")
        self.assertIn("missing_sandbox_tools_allow", ";".join(validation.get("reasons", [])))
        self.assertEqual(self.openclaw_calls(), [])

    def test_strict_reviewer_policy_mode_off_blocks_auditor(self) -> None:
        task_id = "task-auditor-mode-off"
        self.write_issue(task_id)
        self.fake_sandbox_explain = {"sandbox": {"mode": "off", "tools": {"allow": ["read"], "deny": []}}}
        self.set_openclaw_responses({"stdout": self.valid_auditor_output("APPROVE")})

        result = self.run_workspace_script("run-auditor.sh", task_id, "alpha")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        debate_dir = self.debates_dir / task_id
        validation = json.loads((debate_dir / "alpha-validation.json").read_text(encoding="utf-8"))
        self.assertEqual(validation["status"], "invalid_output")
        self.assertIn("sandbox_mode_off", ";".join(validation.get("reasons", [])))
        self.assertEqual(self.openclaw_calls(), [])

    def test_strict_reviewer_policy_workspace_rw_blocks_mediator(self) -> None:
        task_id = "issue-mediator-workspace-rw"
        self.write_issue(task_id)
        debate_dir = self.debates_dir / task_id
        debate_dir.mkdir(parents=True, exist_ok=True)
        self.write_security_auditor_artifacts(debate_dir, task_id)
        (debate_dir / "debate-log.md").write_text("# Debate\n", encoding="utf-8")
        self.fake_sandbox_explain = {"sandbox": {"workspaceAccess": "rw", "tools": {"allow": ["read"], "deny": []}}}
        self.set_openclaw_responses({"stdout": self.valid_mediator_output(task_id, "ACCEPT")})

        result = self.run_workspace_script("run-mediator.sh", task_id)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        final_verdict = json.loads((debate_dir / "final-verdict.json").read_text(encoding="utf-8"))
        self.assertEqual(final_verdict["verdict"], "BLOCK")
        self.assertEqual(final_verdict["consensusType"], "mediator-validation-fail-closed")
        mediator_artifact = (debate_dir / "mediator-ruling.md").read_text(encoding="utf-8")
        self.assertTrue(mediator_artifact.startswith("MEDIATOR_VERDICT: BLOCKED"))
        self.assertIn("sandbox_workspaceAccess_rw", mediator_artifact)
        self.assertEqual(self.openclaw_calls(), [])

    def test_run_mediator_uses_dedicated_readonly_agent_for_policy_and_invocation(self) -> None:
        task_id = "issue-mediator-readonly-agent"
        debate_dir = self.write_valid_mediator_debate(task_id)
        self.set_openclaw_responses({"stdout": self.valid_mediator_output(task_id, "ACCEPT")})

        result = self.run_workspace_script("run-mediator.sh", task_id)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        final_verdict = json.loads((debate_dir / "final-verdict.json").read_text(encoding="utf-8"))
        self.assertEqual(final_verdict["verdict"], "APPROVE")
        self.assertEqual(final_verdict["subject"], task_id)
        self.assertEqual(final_verdict["requestedClaim"], f"security mediation verdict for task {task_id}")
        self.assertEqual(final_verdict["proofTier"], "candidate")
        self.assertEqual(final_verdict["subjectRef"]["branch"], "feature/test")
        self.assertEqual(final_verdict["subjectRef"]["worktree"], str(self.worktree))
        self.assertEqual(final_verdict["reviewRoute"]["reviewProfile"], "security_code")
        self.assertEqual(final_verdict["reviewRoute"]["reviewTier"], "primary_checker_mediator")
        self.assertIn("mediator", final_verdict["reviewRoute"]["completedRoles"])
        self.assert_final_verdict_complete(final_verdict, review_profile="security_code", review_tier="primary_checker_mediator")
        sandbox_calls = self.openclaw_sandbox_calls()
        self.assertEqual(len(sandbox_calls), 1)
        sandbox_argv = list(sandbox_calls[0]["argv"])
        self.assertEqual(sandbox_argv[sandbox_argv.index("--agent") + 1], "mediator-readonly")
        self.assertNotIn("main", sandbox_argv)
        agent_calls = self.openclaw_calls()
        self.assertEqual(len(agent_calls), 1)
        agent_argv = list(agent_calls[0]["argv"])
        self.assertEqual(agent_argv[agent_argv.index("--agent") + 1], "mediator-readonly")
        self.assertNotIn("main", agent_argv)

    def test_run_auditor_security_consensus_final_verdict_is_claim_bound(self) -> None:
        task_id = "issue-auditor-bound-final-verdict"
        self.write_issue(task_id, reviewTier="primary_checker")
        self.set_openclaw_responses(
            {"stdout": self.valid_auditor_output("APPROVE", task_id=task_id)},
            {"stdout": self.valid_auditor_output("APPROVE", task_id=task_id)},
        )

        alpha = self.run_workspace_script("run-auditor.sh", task_id, "alpha")
        self.assertEqual(alpha.returncode, 0, alpha.stderr + alpha.stdout)
        alpha_prime = self.run_workspace_script("run-auditor.sh", task_id, "alpha-prime")
        self.assertEqual(alpha_prime.returncode, 0, alpha_prime.stderr + alpha_prime.stdout)

        final_verdict = json.loads((self.debates_dir / task_id / "final-verdict.json").read_text(encoding="utf-8"))
        self.assertEqual(final_verdict["verdict"], "APPROVE")
        self.assertEqual(final_verdict["subject"], task_id)
        self.assertEqual(final_verdict["requestedClaim"], f"security review verdict for task {task_id}")
        self.assertEqual(final_verdict["proofTier"], "candidate")
        self.assertEqual(final_verdict["subjectRef"]["branch"], "feature/test")
        self.assertEqual(final_verdict["subjectRef"]["worktree"], str(self.worktree))
        self.assertEqual(final_verdict["reviewRoute"]["reviewProfile"], "security_code")
        self.assertEqual(final_verdict["reviewRoute"]["reviewTier"], "primary_checker")
        self.assertEqual(final_verdict["reviewRoute"]["completedRoles"], ["alpha", "alpha-prime"])
        self.assert_final_verdict_complete(final_verdict, review_profile="security_code", review_tier="primary_checker")

    def test_strict_reviewer_policy_rejects_malformed_sandbox_contracts_for_mediator(self) -> None:
        scenarios = (
            ("sandbox_explain_failure", None, None, 7, "sandbox_explain_failed"),
            ("invalid_json", None, "{not-json", 0, "invalid_sandbox_explain_json"),
            ("missing_sandbox", {}, None, 0, "missing_sandbox"),
            ("non_object_sandbox", {"sandbox": "readonly"}, None, 0, "missing_sandbox"),
            ("missing_tools", {"sandbox": {}}, None, 0, "missing_sandbox_tools"),
            ("non_object_tools", {"sandbox": {"tools": []}}, None, 0, "missing_sandbox_tools"),
            ("missing_allow", {"sandbox": {"tools": {"deny": []}}}, None, 0, "missing_sandbox_tools_allow"),
            ("non_array_allow", {"sandbox": {"tools": {"allow": "read", "deny": []}}}, None, 0, "missing_sandbox_tools_allow"),
            (
                "session_not_sandboxed",
                {"sandbox": {"sessionIsSandboxed": False, "tools": {"allow": ["read"], "deny": []}}},
                None,
                0,
                "session_not_sandboxed_for_main_mode:missing",
            ),
        )
        for name, payload, raw_payload, returncode, expected_reason in scenarios:
            with self.subTest(name=name):
                self.fake_sandbox_explain = payload
                self.fake_sandbox_explain_raw = raw_payload
                self.fake_sandbox_explain_returncode = returncode
                task_id = f"issue-mediator-policy-{name}"
                debate_dir = self.write_valid_mediator_debate(task_id)
                self.set_openclaw_responses({"stdout": self.valid_mediator_output(task_id, "ACCEPT")})

                result = self.run_workspace_script("run-mediator.sh", task_id)

                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                final_verdict = json.loads((debate_dir / "final-verdict.json").read_text(encoding="utf-8"))
                self.assertEqual(final_verdict["verdict"], "BLOCK")
                self.assertEqual(final_verdict["consensusType"], "mediator-validation-fail-closed")
                mediator_artifact = (debate_dir / "mediator-ruling.md").read_text(encoding="utf-8")
                self.assertTrue(mediator_artifact.startswith("MEDIATOR_VERDICT: BLOCKED"))
                self.assertIn(expected_reason, mediator_artifact)
                self.assertEqual(self.openclaw_calls(), [])
                sandbox_calls = self.openclaw_sandbox_calls()
                self.assertEqual(len(sandbox_calls), 1)
                sandbox_argv = list(sandbox_calls[0]["argv"])
                self.assertEqual(sandbox_argv[sandbox_argv.index("--agent") + 1], "mediator-readonly")
                self.assertNotIn("main", sandbox_argv)

    def test_review_dispatch_uses_dedicated_generic_reviewer_and_mediator_agents(self) -> None:
        def agent_ids() -> list[str]:
            ids: list[str] = []
            for call in self.openclaw_calls():
                argv = list(call["argv"])
                ids.append(argv[argv.index("--agent") + 1])
            return ids

        task_id = "case-data-agent-id"
        self.write_issue(task_id, reviewProfile="data_validation", reviewTier="single_worker")
        self.set_openclaw_responses({"stdout": self.valid_auditor_output("APPROVE")})
        result = self.run_workspace_script("review-dispatch.sh", task_id)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(agent_ids(), ["reviewer-data"])

        task_id = "case-ops-agent-id"
        self.write_issue(task_id, reviewProfile="ops_pipeline", reviewTier="single_worker")
        self.set_openclaw_responses({"stdout": self.valid_auditor_output("APPROVE")})
        result = self.run_workspace_script("review-dispatch.sh", task_id)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(agent_ids(), ["reviewer-ops"])

        task_id = "case-generic-mediator-agent-id"
        self.write_issue(
            task_id,
            reviewProfile="data_validation",
            reviewTier="primary_checker_mediator",
            reviewRoles={"primary": "primary", "checker": "checker", "mediator": "mediator"},
        )
        self.set_openclaw_responses(
            {"stdout": self.valid_auditor_output("APPROVE")},
            {"stdout": self.valid_auditor_output("APPROVE")},
            {"stdout": self.valid_mediator_output(task_id, "ACCEPT")},
        )
        result = self.run_workspace_script("review-dispatch.sh", task_id)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(agent_ids(), ["reviewer-data", "reviewer-data", "mediator-readonly"])

    def test_review_scripts_reject_traversal_task_ids(self) -> None:
        scenarios = (
            ("review-dispatch.sh", ("../evil",)),
            ("run-auditor.sh", ("../evil", "alpha")),
            ("run-mediator.sh", ("../evil",)),
        )
        for script_name, args in scenarios:
            with self.subTest(script=script_name):
                result = self.run_workspace_script(script_name, *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("invalid task id", result.stderr)
        self.assertFalse((self.workspace / "evil").exists())

    def test_review_dispatch_invalid_generic_reviewer_with_accepting_mediator_blocks(self) -> None:
        scenarios = (
            ("primary", {"stdout": self.raw_source_dump()}, {"stdout": self.valid_auditor_output("APPROVE")}),
            ("checker", {"stdout": self.valid_auditor_output("APPROVE")}, {"stdout": self.raw_source_dump()}),
        )
        for invalid_role, primary_response, checker_response in scenarios:
            with self.subTest(invalid_role=invalid_role):
                task_id = f"case-generic-mediated-invalid-{invalid_role}"
                self.write_issue(
                    task_id,
                    reviewProfile="data_validation",
                    reviewTier="primary_checker_mediator",
                    reviewRoles={"primary": "primary", "checker": "checker", "mediator": "mediator"},
                )
                self.set_openclaw_responses(
                    primary_response,
                    checker_response,
                    {"stdout": self.valid_mediator_output(task_id, "ACCEPT")},
                )

                result = self.run_workspace_script("review-dispatch.sh", task_id)

                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                debate_dir = self.debates_dir / task_id
                final_verdict = json.loads((debate_dir / "final-verdict.json").read_text(encoding="utf-8"))
                self.assertEqual(final_verdict["verdict"], "REQUEST_CHANGES")
                mediator_artifact = (debate_dir / "mediator-ruling.md").read_text(encoding="utf-8")
                self.assertTrue(mediator_artifact.startswith("MEDIATOR_VERDICT: BLOCKED"))
                self.assertIn(f"{invalid_role}_status={invalid_role}_invalid_output", mediator_artifact)
                mediator_validation = json.loads((debate_dir / "mediator-ruling-validation.json").read_text(encoding="utf-8"))
                self.assertEqual(mediator_validation["status"], "accepted")
                self.assertEqual(len(self.openclaw_calls()), 2)

    def test_review_dispatch_invalid_or_failed_generic_mediator_output_is_synthetic(self) -> None:
        mediator_outputs = (
            (
                "unknown_verdict",
                {
                    "stdout": "MEDIATOR_VERDICT: MAYBE\nArtifact path: debates/case-generic-mediator-unknown_verdict/mediator-ruling.md\n\n## Evidence\n- debates/case-generic-mediator-unknown_verdict/reviewer-data-primary.md:1-20\n\n## Decision\nUnknown token.\n"
                },
                "invalid_verdict",
            ),
            ("raw_dump", {"stdout": self.raw_source_dump()}, "raw_source_dump"),
            ("agent_failure", {"stderr": "simulated mediator failure", "returncode": 44}, "mediator agent command failed"),
        )
        for name, mediator_response, expected_reason in mediator_outputs:
            with self.subTest(name=name):
                task_id = f"case-generic-mediator-{name}"
                self.write_issue(
                    task_id,
                    reviewProfile="data_validation",
                    reviewTier="primary_checker_mediator",
                    reviewRoles={"primary": "primary", "checker": "checker", "mediator": "mediator"},
                )
                if name == "unknown_verdict":
                    mediator_response = {
                        "stdout": f"MEDIATOR_VERDICT: MAYBE\nArtifact path: debates/{task_id}/mediator-ruling.md\n\n## Evidence\n- debates/{task_id}/reviewer-data-primary.md:1-20\n\n## Decision\nUnknown token.\n"
                    }
                self.set_openclaw_responses(
                    {"stdout": self.valid_auditor_output("APPROVE")},
                    {"stdout": self.valid_auditor_output("APPROVE")},
                    mediator_response,
                )

                result = self.run_workspace_script("review-dispatch.sh", task_id)

                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                debate_dir = self.debates_dir / task_id
                final_verdict = json.loads((debate_dir / "final-verdict.json").read_text(encoding="utf-8"))
                self.assertEqual(final_verdict["verdict"], "REQUEST_CHANGES")
                mediator_validation = json.loads((debate_dir / "mediator-ruling-validation.json").read_text(encoding="utf-8"))
                self.assertEqual(mediator_validation["status"], "accepted")
                mediator_artifact = (debate_dir / "mediator-ruling.md").read_text(encoding="utf-8")
                self.assertTrue(mediator_artifact.startswith("MEDIATOR_VERDICT: BLOCKED"))
                self.assertIn(expected_reason, mediator_artifact)
                self.assertNotIn("def leaked_source", mediator_artifact)
                self.assertEqual(len(self.openclaw_calls()), 3)

    def test_run_auditor_repeated_malformed_alpha_and_beta_fail_closed(self) -> None:
        for lane in ("alpha", "beta"):
            with self.subTest(lane=lane):
                task_id = f"task-malformed-{lane}"
                self.write_issue(task_id)
                self.set_openclaw_responses(
                    {"stdout": self.raw_source_dump()},
                    {"stdout": self.raw_source_dump()},
                )

                result = self.run_workspace_script("run-auditor.sh", task_id, lane)

                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                debate_dir = self.debates_dir / task_id
                validation = json.loads((debate_dir / f"{lane}-validation.json").read_text(encoding="utf-8"))
                self.assertEqual(validation["status"], "invalid_output")
                self.assertEqual(validation["action"], "fail_closed")
                self.assertIn("raw_source_dump", validation["reasons"])
                self.assertFalse((debate_dir / f"{lane}.json").exists())
                self.assertIn("Invalid auditor output", (debate_dir / ("alpha-initial.md" if lane == "alpha" else "beta-initial.md")).read_text(encoding="utf-8"))
                self.assertIn("invalid_output", (debate_dir / "debate-log.md").read_text(encoding="utf-8"))
                self.assertEqual(len(self.openclaw_calls()), 2)

    def test_run_auditor_unsafe_tool_policy_fails_closed_before_agent(self) -> None:
        task_id = "issue-auditor-unsafe-policy"
        self.write_issue(task_id)
        self.fake_sandbox_explain = {"sandbox": {"tools": {"allow": ["read", "process", "apply_patch"], "deny": []}}}
        self.set_openclaw_responses({"stdout": self.valid_auditor_output("APPROVE")})

        result = self.run_workspace_script("run-auditor.sh", task_id, "alpha")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        debate_dir = self.debates_dir / task_id
        validation = json.loads((debate_dir / "alpha-validation.json").read_text(encoding="utf-8"))
        self.assertEqual(validation["status"], "invalid_output")
        self.assertEqual(validation["action"], "fail_closed")
        self.assertIn("unsafe_reviewer_tool_policy", ";".join(validation.get("reasons", [])))
        self.assertFalse((debate_dir / "alpha.json").exists())
        self.assertEqual(self.openclaw_calls(), [])

    def test_run_mediator_revalidates_beta_and_blocks_malformed_auditor_evidence(self) -> None:
        task_id = "task-beta-malformed"
        self.write_issue(task_id)
        debate_dir = self.debates_dir / task_id
        debate_dir.mkdir(parents=True, exist_ok=True)
        for name, role in (
            ("alpha-initial.md", "alpha"),
            ("alpha-prime-initial.md", "alpha-prime"),
        ):
            (debate_dir / name).write_text(
                self.valid_auditor_output(
                    task_id=task_id,
                    review_profile="security_code",
                    review_tier="primary_checker_mediator",
                    role=role,
                ),
                encoding="utf-8",
            )
        (debate_dir / "beta-initial.md").write_text("Ran 3 tests in 0.01s\n\nOK\n", encoding="utf-8")
        (debate_dir / "debate-log.md").write_text("# Debate\n", encoding="utf-8")
        self.set_openclaw_responses()

        result = self.run_workspace_script("run-mediator.sh", task_id)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        final_verdict = json.loads((debate_dir / "final-verdict.json").read_text(encoding="utf-8"))
        self.assertEqual(final_verdict["verdict"], "BLOCK")
        self.assertEqual(final_verdict["consensusType"], "auditor-validation-fail-closed")
        beta_validation = json.loads((debate_dir / "beta-validation.json").read_text(encoding="utf-8"))
        self.assertEqual(beta_validation["status"], "invalid_output")
        self.assertIn("raw_test_dump", beta_validation["reasons"])
        self.assertTrue((debate_dir / "mediator-ruling.md").read_text(encoding="utf-8").startswith("MEDIATOR_VERDICT: BLOCKED"))
        self.assertEqual(self.openclaw_calls(), [])

    def test_run_mediator_unsafe_tool_policy_blocks_before_agent(self) -> None:
        task_id = "issue-mediator-unsafe-policy"
        self.write_issue(task_id)
        debate_dir = self.debates_dir / task_id
        debate_dir.mkdir(parents=True, exist_ok=True)
        self.write_security_auditor_artifacts(debate_dir, task_id)
        (debate_dir / "debate-log.md").write_text("# Debate\n", encoding="utf-8")
        self.fake_sandbox_explain = {"sandbox": {"tools": {"allow": ["read", "exec"], "deny": []}}}
        self.set_openclaw_responses({"stdout": self.valid_mediator_output(task_id, "ACCEPT")})

        result = self.run_workspace_script("run-mediator.sh", task_id)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        final_verdict = json.loads((debate_dir / "final-verdict.json").read_text(encoding="utf-8"))
        self.assertEqual(final_verdict["verdict"], "BLOCK")
        self.assertEqual(final_verdict["consensusType"], "mediator-validation-fail-closed")
        self.assertTrue((debate_dir / "mediator-ruling.md").read_text(encoding="utf-8").startswith("MEDIATOR_VERDICT: BLOCKED"))
        self.assertEqual(self.openclaw_calls(), [])

    def test_run_mediator_repeated_malformed_verdicts_fail_closed(self) -> None:
        task_id = "task-mediator-malformed"
        self.write_issue(task_id)
        debate_dir = self.debates_dir / task_id
        debate_dir.mkdir(parents=True, exist_ok=True)
        self.write_security_auditor_artifacts(debate_dir, task_id)
        (debate_dir / "debate-log.md").write_text("# Debate\n", encoding="utf-8")
        self.set_openclaw_responses(
            {"stdout": "## Evidence\n- debates/task-mediator-malformed/alpha-initial.md:1-20\n\n## Decision\nApprove without required verdict.\n"},
            {"stdout": "MEDIATOR_VERDICT: MAYBE\nArtifact path: debates/task-mediator-malformed/mediator-ruling.md\n\n## Evidence\n- debates/task-mediator-malformed/alpha-initial.md:1-20\n\n## Decision\nUnknown verdict token.\n"},
        )

        result = self.run_workspace_script("run-mediator.sh", task_id)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        final_verdict = json.loads((debate_dir / "final-verdict.json").read_text(encoding="utf-8"))
        self.assertEqual(final_verdict["verdict"], "BLOCK")
        self.assertEqual(final_verdict["consensusType"], "mediator-validation-fail-closed")
        self.assertIn("invalid after repair", final_verdict["summary"])
        mediator_artifact = (debate_dir / "mediator-ruling.md").read_text(encoding="utf-8")
        self.assertTrue(mediator_artifact.startswith("MEDIATOR_VERDICT: BLOCKED"))
        self.assertNotIn("Approve without required verdict", mediator_artifact)
        self.assertNotIn("Unknown verdict token", mediator_artifact)
        self.assertEqual(len(self.openclaw_calls()), 2)

    def test_review_dispatch_missing_generic_mediator_output_never_approves(self) -> None:
        task_id = "case-generic-missing-mediator"
        self.write_issue(
            task_id,
            reviewProfile="data_validation",
            reviewTier="primary_checker_mediator",
            reviewRoles={"primary": "primary", "checker": "checker", "mediator": "mediator"},
        )
        self.set_openclaw_responses(
            {"stdout": self.valid_auditor_output("APPROVE")},
            {"stdout": self.valid_auditor_output("APPROVE")},
            {"stdout": ""},
        )

        result = self.run_workspace_script("review-dispatch.sh", task_id)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        debate_dir = self.debates_dir / task_id
        final_verdict = json.loads((debate_dir / "final-verdict.json").read_text(encoding="utf-8"))
        self.assertEqual(final_verdict["verdict"], "REQUEST_CHANGES")
        mediator_validation = json.loads((debate_dir / "mediator-ruling-validation.json").read_text(encoding="utf-8"))
        self.assertEqual(mediator_validation["status"], "accepted")
        mediator_artifact = (debate_dir / "mediator-ruling.md").read_text(encoding="utf-8")
        self.assertTrue(mediator_artifact.startswith("MEDIATOR_VERDICT: BLOCKED"))
        self.assertIn("empty_output", mediator_artifact)
        self.assertEqual(len(self.openclaw_calls()), 3)

    def test_dispatch_issue_writes_extra_high_and_subagent_bounds(self) -> None:
        task_id = "task-dispatch-bounds"
        issue_workdir = self.root / "issue-workdir"
        issue_workdir.mkdir(parents=True, exist_ok=True)
        self.write_issue(task_id, workspace=str(issue_workdir), workerMode="code", branch="feature/bounds")
        self.authorize_dispatch_issue(task_id)
        for helper in ("write-issue-summary.py", "render-state.py", "render-active-tasks.py"):
            helper_path = self.scripts_dir / helper
            helper_path.write_text("#!/usr/bin/env python3\nraise SystemExit(0)\n", encoding="utf-8")
            helper_path.chmod(0o755)
        tmux = self.bin_dir / "tmux"
        tmux.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$1\" == \"has-session\" ]]; then exit 1; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        tmux.chmod(0o755)

        result = self.run_workspace_script("dispatch-issue.sh", task_id)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        contract = (issue_workdir / ".issue-contract.md").read_text(encoding="utf-8")
        self.assertIn('- thinking: "extra-high"', contract)
        self.assertIn("subagent.context: isolated", contract)
        self.assertIn("subagent.maxConcurrency: 1", contract)
        issue = json.loads((self.issues_dir / f"{task_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(issue["thinking"], "extra-high")
        self.assertEqual(issue["subagentBounds"]["context"], "isolated")
        self.assertEqual(issue["subagentBounds"]["maxConcurrency"], 1)
        self.assertEqual(issue["agents"]["coder"]["thinking"], "extra-high")
        self.assertEqual(issue["agents"]["coder"]["context"], "isolated")


class OrchestratorSubagentBoundsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "state" / "issues").mkdir(parents=True, exist_ok=True)
        self.tick = load_module("test_orchestrator_tick_bounds", REPO_ROOT / "scripts" / "orchestrator-tick.py")
        self.tick.ROOT = self.root
        self.tick.STATE_DIR = self.root / "state"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def assert_subagent_bounds(self, issue: dict[str, object]) -> None:
        self.assertEqual(issue.get("thinking"), "extra-high")
        bounds = issue.get("subagentBounds")
        self.assertIsInstance(bounds, dict)
        assert isinstance(bounds, dict)
        self.assertEqual(bounds.get("context"), "isolated")
        self.assertEqual(bounds.get("maxConcurrency"), 1)
        self.assertIn("timeout", bounds)
        self.assertIn("cleanupArchive", bounds)

    def test_orchestrator_generated_helper_issues_carry_extra_high_bounds(self) -> None:
        parent = {
            "id": "opt-parent",
            "project": "proj",
            "parent": "epic-a",
            "title": "Optimize parent",
            "researchAction": "candidate_generation",
        }
        helpers = [
            self.tick.build_optimize_lane_issue(
                parent,
                issue_id="opt-parent-exploit",
                lane_id="exploit",
                lane_role="exploit",
                hypothesis_family="family-a",
                workspace=self.root / "lanes" / "exploit",
            ),
            self.tick.build_finalization_issue(
                parent,
                issue_id="opt-parent-final",
                lane_id="final",
                lane_role="finalization",
                research_action="final_validation",
                workspace=self.root / "lanes" / "final",
            ),
            self.tick.build_truth_repair_issue(
                parent,
                issue_id="opt-parent-truth-repair",
                failure_source="validator",
                failure_detail="missing proof",
            ),
        ]
        for issue in helpers:
            with self.subTest(issue=issue["id"]):
                self.assert_subagent_bounds(issue)

    def test_remaining_generated_child_issue_builders_carry_extra_high_bounds(self) -> None:
        parent = {
            "id": "opt-parent",
            "project": "proj",
            "parent": "epic-a",
            "title": "Optimize parent",
            "researchAction": "candidate_generation",
        }
        research_ctx = {
            "enabled": True,
            "program": {},
            "state": {
                "currentChampionId": "champion-a",
                "flatline": {"playbook": {"runSeq": 2}},
                "distillation": {
                    "sequence": 3,
                    "pendingEvents": [
                        {"event": "candidate_improved", "timestamp": "2026-05-12T00:00:00Z", "message": "new lesson"}
                    ],
                },
                "evaluatorExploitReview": {
                    "status": "pending",
                    "sequence": 4,
                    "pendingReason": "score_jump",
                    "pendingChampionId": "champion-a",
                    "pendingChampionMetric": 0.91,
                    "pendingScoreJumpPct": 12.5,
                },
            },
        }
        flatline_step = {
            "id": "literature_refresh",
            "label": "Literature refresh",
            "budget": {"maxIterations": 1, "timeboxMinutes": 20, "tokenBudget": 6000},
        }
        helpers = [
            self.tick.build_flatline_entropy_bridge_issue(
                parent,
                issue_id="opt-parent-flatline-bridge",
                step=flatline_step,
                step_index=1,
                total_steps=4,
                research_ctx=research_ctx,
            ),
            self.tick.build_research_critic_issue(
                parent,
                issue_id="opt-parent-critic",
            ),
            self.tick.build_living_distillation_issue(
                parent,
                issue_id="opt-parent-distillation-3",
                research_ctx=research_ctx,
            ),
            self.tick.build_evaluator_exploit_review_issue(
                parent,
                issue_id="opt-parent-evaluator-exploit-score-jump-4",
                research_ctx=research_ctx,
            ),
        ]
        for issue in helpers:
            with self.subTest(issue=issue["id"]):
                self.assert_subagent_bounds(issue)

    def test_materialized_retrospective_issue_and_contract_carry_bounds(self) -> None:
        source = {"id": "source-issue", "project": "(framework)", "title": "Source", "state": "Done"}
        (self.root / "state" / "issues" / "source-issue.json").write_text(json.dumps(source, indent=2) + "\n", encoding="utf-8")
        request = {
            "id": "retro-source-issue",
            "project": "(framework)",
            "sourceIssueIds": ["source-issue"],
            "reason": "completion",
            "triggerKey": "completion:source-issue",
        }

        issue = self.tick.materialize_retrospective_issue(request, {"source-issue": source})

        self.assertIsNotNone(issue)
        assert issue is not None
        self.assert_subagent_bounds(issue)
        contract = Path(issue["workspace"]).joinpath(".issue-contract.md").read_text(encoding="utf-8")
        self.assertIn('- thinking: "extra-high"', contract)
        self.assertIn("subagent.context: isolated", contract)
        self.assertIn("subagent.maxConcurrency: 1", contract)


if __name__ == "__main__":
    unittest.main()
