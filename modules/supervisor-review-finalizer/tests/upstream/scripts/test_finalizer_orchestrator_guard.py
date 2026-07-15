#!/usr/bin/env python3
"""Focused regressions for required finalizer transition guards."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FinalizerOrchestratorGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_home = Path(tempfile.mkdtemp(prefix="openclaw-finalizer-guard-"))
        self.openclaw = self.temp_home / ".openclaw"
        shutil.copytree(
            REPO_ROOT,
            self.openclaw / "workspace",
            ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "node_modules", "projects", "tmp", "uploads", "*.zip"),
            ignore_dangling_symlinks=True,
        )
        shutil.rmtree(self.workspace / "state", ignore_errors=True)
        (self.workspace / "state" / "issues").mkdir(parents=True, exist_ok=True)
        self.env = dict(
            os.environ,
            HOME=str(self.temp_home),
            PYTHONDONTWRITEBYTECODE="1",
            GIT_AUTHOR_NAME="OpenClaw Test",
            GIT_AUTHOR_EMAIL="openclaw-test@example.com",
            GIT_COMMITTER_NAME="OpenClaw Test",
            GIT_COMMITTER_EMAIL="openclaw-test@example.com",
        )
        self.original_sys_path = list(sys.path)
        sys.path.insert(0, str(self.workspace / "scripts"))

    def tearDown(self) -> None:
        sys.path[:] = self.original_sys_path
        shutil.rmtree(self.temp_home, ignore_errors=True)

    @property
    def workspace(self) -> Path:
        return self.openclaw / "workspace"

    def project_dir(self, name: str) -> Path:
        return self.openclaw / "projects" / name

    def exec_cmd(self, *cmd: str, cwd: Path | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(cmd),
            cwd=str(cwd) if cwd else None,
            env=self.env,
            text=True,
            capture_output=True,
            check=check,
        )

    def write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def read_json(self, path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def write_issue(self, issue_id: str, payload: dict[str, Any]) -> None:
        payload.setdefault("id", issue_id)
        self.write_json(self.workspace / "state" / "issues" / f"{issue_id}.json", payload)

    def append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")

    def init_project_repo(self, project: str) -> None:
        origin = self.temp_home / "origins" / f"{project}.git"
        origin.parent.mkdir(parents=True, exist_ok=True)
        self.exec_cmd("git", "init", "--bare", str(origin), check=True)
        pdir = self.project_dir(project)
        pdir.parent.mkdir(parents=True, exist_ok=True)
        self.exec_cmd("git", "clone", str(origin), str(pdir), check=True)
        current = self.exec_cmd("git", "-C", str(pdir), "symbolic-ref", "--short", "HEAD", check=True).stdout.strip()
        if current != "main":
            self.exec_cmd("git", "-C", str(pdir), "checkout", "-b", "main", check=True)
        (pdir / "README.md").write_text("# project\n", encoding="utf-8")
        self.exec_cmd("git", "-C", str(pdir), "add", "README.md", check=True)
        self.exec_cmd("git", "-C", str(pdir), "commit", "-m", "init", check=True)
        self.exec_cmd("git", "-C", str(pdir), "push", "-u", "origin", "main", check=True)
        self.exec_cmd("git", "-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/main", check=True)
        self.exec_cmd("git", "-C", str(pdir), "remote", "set-head", "origin", "main", check=True)

    def create_feature_branch(self, project: str, branch: str) -> str:
        pdir = self.project_dir(project)
        self.exec_cmd("git", "-C", str(pdir), "checkout", "-b", branch, check=True)
        (pdir / "README.md").write_text("feature\n", encoding="utf-8")
        self.exec_cmd("git", "-C", str(pdir), "commit", "-am", "feature", check=True)
        self.exec_cmd("git", "-C", str(pdir), "push", "-u", "origin", branch, check=True)
        return self.exec_cmd("git", "-C", str(pdir), "rev-parse", "HEAD", check=True).stdout.strip()

    def finalizer_evidence(self, commit: str = "abc123") -> dict[str, Any]:
        return {
            "ok": True,
            "status": "passed",
            "subjectCommitOid": commit,
            "destinationRef": "refs/heads/main",
            "changedPaths": ["scripts/finalizer_required.py"],
            "stagedPaths": ["scripts/finalizer_required.py"],
            "errors": [],
        }

    def test_done_transition_blocks_without_required_finalizer_evidence(self) -> None:
        project = "proj-finalizer-done"
        issue_id = "issue-finalizer-done"
        branch = "feature/finalizer-done"
        self.init_project_repo(project)
        feature_commit = self.create_feature_branch(project, branch)
        main_before = self.exec_cmd("git", "-C", str(self.project_dir(project)), "rev-parse", "main", check=True).stdout.strip()
        self.write_issue(
            issue_id,
            {
                "project": project,
                "state": "Merging",
                "workspace": str(self.project_dir(project)),
                "branch": branch,
                "finalizerRequired": True,
                "allowedPaths": ["scripts/finalizer_required.py"],
            },
        )
        self.write_json(
            self.workspace / "debates" / issue_id / "final-verdict.json",
            {"verdict": "APPROVE", "commit": feature_commit},
        )
        self.append_jsonl(self.workspace / "state" / "evals" / f"{issue_id}.jsonl", {"status": "pass", "final": True, "commit": feature_commit})

        result = self.exec_cmd("bash", str(self.workspace / "scripts" / "auto-merge.sh"), issue_id)

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        issue = self.read_json(self.workspace / "state" / "issues" / f"{issue_id}.json")
        self.assertEqual(issue["state"], "Blocked")
        self.assertIn("FINALIZER_REQUIRED", issue["status"]["blockedReason"])
        main_after = self.exec_cmd("git", "-C", str(self.project_dir(project)), "rev-parse", "main", check=True).stdout.strip()
        self.assertEqual(main_after, main_before)

    def test_verify_handoff_blocks_human_review_without_finalizer(self) -> None:
        workdir = self.temp_home / "verify-workdir"
        workdir.mkdir(parents=True, exist_ok=True)
        self.write_json(workdir / ".issue-status.json", {"handoffReady": True})
        issue_id = "fwk-chh-finalizer-verify"
        self.write_issue(
            issue_id,
            {
                "project": "(framework)",
                "state": "Todo",
                "workspace": str(workdir),
                "allowedPaths": ["scripts/verify-issue-handoff.py"],
            },
        )

        result = self.exec_cmd("python3", str(self.workspace / "scripts" / "verify-issue-handoff.py"), issue_id)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["finalizer"]["ok"])
        self.assertIn("FINALIZER_REQUIRED", "\n".join(payload["reasons"]))

    def test_human_review_is_not_sufficient_when_finalizer_required(self) -> None:
        project = "proj-finalizer-review"
        issue_id = "issue-finalizer-review"
        self.init_project_repo(project)
        commit = self.exec_cmd("git", "-C", str(self.project_dir(project)), "rev-parse", "HEAD", check=True).stdout.strip()
        self.write_issue(
            issue_id,
            {
                "project": project,
                "state": "Human Review",
                "workspace": str(self.project_dir(project)),
                "branch": "main",
                "finalizerRequired": True,
                "allowedPaths": ["scripts/finalizer_required.py"],
            },
        )
        for reviewer in ("alpha", "alpha-prime", "beta"):
            self.write_json(self.workspace / "debates" / issue_id / f"{reviewer}.json", {"findings": []})
        self.write_json(self.workspace / "debates" / issue_id / "final-verdict.json", {"verdict": "APPROVE", "commit": commit})
        self.write_json(self.workspace / "state" / "supervisor" / "config.json", {"autoMergeOnApproval": False})

        result = self.exec_cmd("bash", str(self.workspace / "scripts" / "supervisor-evaluate.sh"), issue_id)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        issue = self.read_json(self.workspace / "state" / "issues" / f"{issue_id}.json")
        self.assertEqual(issue["state"], "Blocked")
        self.assertIn("FINALIZER_REQUIRED", issue["status"]["blockedReason"])
        self.assertNotIn("approvedAt", issue.get("status") or {})
        escalation = self.read_json(self.workspace / "state" / "supervisor" / "escalations" / f"{issue_id}.json")
        self.assertEqual(escalation["invariant"], "I-FINALIZER")
        self.assertIn("FINALIZER_REQUIRED", escalation["reason"])

    def test_supervisor_finalizer_escalation_json_escapes_multiline_reason(self) -> None:
        project = "proj-finalizer-json"
        issue_id = "issue-finalizer-json"
        self.init_project_repo(project)
        commit = self.exec_cmd("git", "-C", str(self.project_dir(project)), "rev-parse", "HEAD", check=True).stdout.strip()
        self.write_issue(
            issue_id,
            {
                "project": project,
                "state": "Human Review",
                "workspace": str(self.project_dir(project)),
                "branch": "main",
                "finalizerRequired": True,
                "allowedPaths": ["scripts/finalizer_required.py"],
                "status": {"finalizer": {"ok": True, "status": "passed", "errors": ["line1\nline2"]}},
            },
        )
        for reviewer in ("alpha", "alpha-prime", "beta"):
            self.write_json(self.workspace / "debates" / issue_id / f"{reviewer}.json", {"findings": []})
        self.write_json(self.workspace / "debates" / issue_id / "final-verdict.json", {"verdict": "APPROVE", "commit": commit})
        self.write_json(self.workspace / "state" / "supervisor" / "config.json", {"autoMergeOnApproval": False})

        result = self.exec_cmd("bash", str(self.workspace / "scripts" / "supervisor-evaluate.sh"), issue_id)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        escalation = self.read_json(self.workspace / "state" / "supervisor" / "escalations" / f"{issue_id}.json")
        self.assertEqual(escalation["invariant"], "I-FINALIZER")
        self.assertIn("FINALIZER_REQUIRED", escalation["reason"])

    def test_finalizer_evidence_rejects_raw_evidence_shaped_keys(self) -> None:
        helper = load_module("finalizer_required_raw_key_test_module", self.workspace / "scripts" / "finalizer_required.py")
        issue = {
            "id": "raw-evidence",
            "project": "(framework)",
            "state": "Done",
            "finalizerRequired": True,
            "allowedPaths": ["scripts/finalizer_required.py"],
            "status": {
                "finalizer": {
                    "ok": True,
                    "status": "passed",
                    "subjectCommitOid": "abc123",
                    "destinationRef": "refs/heads/main",
                    "changedPaths": ["scripts/finalizer_required.py"],
                    "errors": [],
                    "rawDiff": "SENTINEL_RAW_DIFF_SHOULD_NOT_BE_ACCEPTED",
                }
            },
        }

        result = helper.finalizer_transition_guard(issue, "Done")

        self.assertFalse(result["ok"])
        self.assertIn("raw/log/diff/env/auth/secret/session/device-shaped", result["reason"])
        self.assertIn("rawDiff", result["forbiddenEvidenceKeys"])

    def test_finalizer_evidence_paths_must_be_within_issue_allowed_paths(self) -> None:
        helper = load_module("finalizer_required_offscope_path_test_module", self.workspace / "scripts" / "finalizer_required.py")
        issue = {
            "id": "offscope-evidence",
            "project": "(framework)",
            "state": "Done",
            "finalizerRequired": True,
            "allowedPaths": ["docs/allowed.md"],
            "status": {
                "finalizer": {
                    "ok": True,
                    "status": "passed",
                    "subjectCommitOid": "abc123",
                    "destinationRef": "refs/heads/main",
                    "changedPaths": ["scripts/offscope.py"],
                    "errors": [],
                }
            },
        }

        result = helper.finalizer_transition_guard(issue, "Done")

        self.assertFalse(result["ok"])
        self.assertIn("outside issue.allowedPaths", result["reason"])
        self.assertEqual(result["offScopePaths"], ["scripts/offscope.py"])

    def test_local_only_does_not_bypass_raw_evidence_keys_or_invalid_issue_scope(self) -> None:
        helper = load_module("finalizer_required_localonly_bypass_test_module", self.workspace / "scripts" / "finalizer_required.py")
        raw_issue = {
            "id": "local-raw",
            "project": "(framework)",
            "state": "Done",
            "allowedPaths": ["docs/local-note.md"],
            "status": {
                "finalizer": {
                    "localOnly": {
                        "paths": ["docs/local-note.md"],
                        "rationale": "documentation-only local note",
                    },
                    "rawDiff": "SHOULD_NOT_BE_ACCEPTED",
                }
            },
        }
        raw_result = helper.finalizer_transition_guard(raw_issue, "Done")
        self.assertFalse(raw_result["ok"])
        self.assertIn("raw/log/diff/env/auth/secret/session/device-shaped", raw_result["reason"])

        glob_scope_issue = {
            "id": "local-glob-scope",
            "project": "(framework)",
            "state": "Done",
            "allowedPaths": ["docs/*.md"],
            "status": {
                "finalizer": {
                    "localOnly": {
                        "paths": ["docs/local-note.md"],
                        "rationale": "documentation-only local note",
                    }
                }
            },
        }
        glob_result = helper.finalizer_transition_guard(glob_scope_issue, "Done")
        self.assertFalse(glob_result["ok"])
        self.assertIn("path contains glob/wildcard syntax", glob_result["reason"])

        control_scope_issue = {
            "id": "local-control-scope",
            "project": "(framework)",
            "state": "Done",
            "allowedPaths": ["scripts/finalizer_required.py"],
            "status": {
                "finalizer": {
                    "localOnly": {
                        "paths": ["scripts/finalizer_required.py"],
                        "rationale": "documentation-only local note",
                    }
                }
            },
        }
        control_result = helper.finalizer_transition_guard(control_scope_issue, "Done")
        self.assertFalse(control_result["ok"])
        self.assertIn("cannot include harness/control-plane paths", control_result["reason"])

        top_level_offscope_issue = {
            "id": "local-top-level-offscope",
            "project": "(framework)",
            "state": "Done",
            "allowedPaths": ["docs/local-note.md"],
            "status": {
                "finalizer": {
                    "changedPaths": ["scripts/finalizer_required.py"],
                    "localOnly": {
                        "paths": ["docs/local-note.md"],
                        "rationale": "documentation-only local note",
                    },
                }
            },
        }
        top_level_result = helper.finalizer_transition_guard(top_level_offscope_issue, "Done")
        self.assertFalse(top_level_result["ok"])
        self.assertIn("outside issue.allowedPaths", top_level_result["reason"])
        self.assertEqual(top_level_result["offScopePaths"], ["scripts/finalizer_required.py"])

        local_field_offscope_issue = {
            "id": "local-field-offscope",
            "project": "(framework)",
            "state": "Done",
            "allowedPaths": ["docs/local-note.md"],
            "status": {
                "finalizer": {
                    "localOnly": {
                        "paths": ["docs/local-note.md"],
                        "path": "scripts/finalizer_required.py",
                        "changedPaths": ["scripts/finalizer_required.py"],
                        "stagedPaths": ["docs/local-note.md"],
                        "wouldStagePaths": ["docs/local-note.md"],
                        "manifestPaths": ["docs/local-note.md"],
                        "rationale": "documentation-only local note",
                    }
                }
            },
        }
        local_field_result = helper.finalizer_transition_guard(local_field_offscope_issue, "Done")
        self.assertFalse(local_field_result["ok"])
        self.assertIn("outside issue.allowedPaths", local_field_result["reason"])
        self.assertEqual(local_field_result["offScopePaths"], ["scripts/finalizer_required.py"])

        top_level_alias_issue = {
            "id": "local-top-level-alias-offscope",
            "project": "(framework)",
            "state": "Done",
            "allowedPaths": ["docs/local-note.md"],
            "status": {
                "finalizer": {
                    "paths": ["scripts/finalizer_required.py"],
                    "localOnly": {
                        "paths": ["docs/local-note.md"],
                        "rationale": "documentation-only local note",
                    },
                }
            },
        }
        top_level_alias_result = helper.finalizer_transition_guard(top_level_alias_issue, "Done")
        self.assertFalse(top_level_alias_result["ok"])
        self.assertIn("outside issue.allowedPaths", top_level_alias_result["reason"])
        self.assertEqual(top_level_alias_result["offScopePaths"], ["scripts/finalizer_required.py"])

        no_allowed_top_level_control_issue = {
            "id": "local-no-allowed-top-control",
            "project": "(framework)",
            "state": "Done",
            "status": {
                "finalizer": {
                    "paths": ["scripts/finalizer_required.py"],
                    "localOnly": {
                        "paths": ["docs/local-note.md"],
                        "rationale": "documentation-only local note",
                    },
                }
            },
        }
        no_allowed_top_result = helper.finalizer_transition_guard(no_allowed_top_level_control_issue, "Done")
        self.assertFalse(no_allowed_top_result["ok"])
        self.assertIn("cannot include harness/control-plane paths", no_allowed_top_result["reason"])
        self.assertEqual(no_allowed_top_result["controlPlanePaths"], ["scripts/finalizer_required.py"])

        no_allowed_local_control_issue = {
            "id": "local-no-allowed-local-control",
            "project": "(framework)",
            "state": "Done",
            "status": {
                "finalizer": {
                    "localOnly": {
                        "path": "scripts/finalizer_required.py",
                        "paths": ["docs/local-note.md"],
                        "rationale": "documentation-only local note",
                    }
                }
            },
        }
        no_allowed_local_result = helper.finalizer_transition_guard(no_allowed_local_control_issue, "Done")
        self.assertFalse(no_allowed_local_result["ok"])
        self.assertIn("cannot include harness/control-plane paths", no_allowed_local_result["reason"])

    def test_not_required_does_not_bypass_present_finalizer_path_metadata(self) -> None:
        helper = load_module("finalizer_required_not_required_scope_test_module", self.workspace / "scripts" / "finalizer_required.py")
        offscope_issue = {
            "id": "not-required-offscope",
            "project": "(framework)",
            "state": "Done",
            "finalizerNotRequired": True,
            "allowedPaths": ["docs/local-note.md"],
            "status": {
                "finalizer": {
                    "localOnly": {
                        "paths": ["docs/local-note.md"],
                        "rationale": "documentation-only local note",
                    },
                    "changedPaths": ["scripts/finalizer_required.py"],
                    "stagedPaths": ["docs/local-note.md"],
                    "wouldStagePaths": ["docs/local-note.md"],
                    "manifestPaths": ["docs/local-note.md"],
                }
            },
        }
        offscope_result = helper.finalizer_transition_guard(offscope_issue, "Done")
        self.assertFalse(offscope_result["ok"])
        self.assertIn("outside issue.allowedPaths", offscope_result["reason"])
        self.assertEqual(offscope_result["offScopePaths"], ["scripts/finalizer_required.py"])

        invalid_local_issue = {
            "id": "not-required-invalid-local",
            "project": "(framework)",
            "state": "Done",
            "finalizerNotRequired": True,
            "allowedPaths": ["docs/local-note.md"],
            "status": {
                "finalizer": {
                    "localOnly": {
                        "paths": ["docs/local-note.md"],
                        "changedPaths": ["scripts/finalizer_required.py"],
                        "rationale": "documentation-only local note",
                    }
                }
            },
        }
        invalid_local_result = helper.finalizer_transition_guard(invalid_local_issue, "Done")
        self.assertFalse(invalid_local_result["ok"])
        self.assertIn("outside issue.allowedPaths", invalid_local_result["reason"])

        no_allowed_control_issue = {
            "id": "not-required-no-allowed-control",
            "project": "(framework)",
            "state": "Done",
            "finalizerNotRequired": True,
            "status": {
                "finalizer": {
                    "paths": ["scripts/finalizer_required.py"],
                    "localOnly": {
                        "paths": ["docs/local-note.md"],
                        "rationale": "documentation-only local note",
                    },
                }
            },
        }
        no_allowed_control_result = helper.finalizer_transition_guard(no_allowed_control_issue, "Done")
        self.assertFalse(no_allowed_control_result["ok"])
        self.assertIn("cannot include harness/control-plane paths", no_allowed_control_result["reason"])
        self.assertEqual(no_allowed_control_result["controlPlanePaths"], ["scripts/finalizer_required.py"])

    def test_epic_auto_advance_blocks_when_required_child_lacks_finalizer(self) -> None:
        self.write_issue(
            "epic-a",
            {"kind": "epic", "project": "(framework)", "state": "Todo", "children": ["task-a"]},
        )
        self.write_issue(
            "task-a",
            {
                "kind": "task",
                "project": "(framework)",
                "parent": "epic-a",
                "state": "Done",
                "allowedPaths": ["scripts/orchestrator-tick.py"],
            },
        )

        result = self.exec_cmd("python3", str(self.workspace / "scripts" / "advance-epics.py"))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        epic = self.read_json(self.workspace / "state" / "issues" / "epic-a.json")
        self.assertEqual(epic["state"], "Todo")
        self.assertIn("FINALIZER_REQUIRED", epic["status"]["finalizerBlockedReason"])

    def test_state_set_ready_blocks_authorized_scope_missing_finalizer(self) -> None:
        self.write_issue(
            "epic-state-set",
            {"kind": "epic", "project": "(framework)", "state": "Human Review", "children": ["task-state-set"]},
        )
        self.write_issue(
            "task-state-set",
            {
                "kind": "task",
                "project": "(framework)",
                "parent": "epic-state-set",
                "state": "Done",
                "allowedPaths": ["scripts/state-set.sh"],
            },
        )
        self.write_json(
            self.workspace / "state" / "orchestrator.json",
            {
                "schemaVersion": 1,
                "activeProject": "(framework)",
                "phase": "executing",
                "authorizedEpic": "epic-state-set",
                "maxConcurrentWorkers": 1,
                "runningIssues": [],
                "blockedIssues": [],
            },
        )

        result = self.exec_cmd("bash", str(self.workspace / "scripts" / "state-set.sh"), "--phase", "ready", "--clearAuthorizedEpic")

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Refusing READY transition", result.stderr)
        orch = self.read_json(self.workspace / "state" / "orchestrator.json")
        self.assertEqual(orch["phase"], "executing")
        self.assertEqual(orch["authorizedEpic"], "epic-state-set")

    def test_state_set_ready_checks_authorized_epic_finalizer_not_only_children(self) -> None:
        self.write_issue(
            "epic-state-set-self",
            {
                "kind": "epic",
                "project": "(framework)",
                "state": "Human Review",
                "children": ["task-state-set-self"],
                "finalizerRequired": True,
                "allowedPaths": ["scripts/state-set.sh"],
            },
        )
        self.write_issue(
            "task-state-set-self",
            {
                "kind": "task",
                "project": "(framework)",
                "parent": "epic-state-set-self",
                "state": "Cancelled",
                "required": False,
            },
        )
        self.write_json(
            self.workspace / "state" / "orchestrator.json",
            {
                "schemaVersion": 1,
                "activeProject": "(framework)",
                "phase": "executing",
                "authorizedEpic": "epic-state-set-self",
                "maxConcurrentWorkers": 1,
                "runningIssues": [],
                "blockedIssues": [],
            },
        )

        result = self.exec_cmd("bash", str(self.workspace / "scripts" / "state-set.sh"), "--phase", "ready", "--clearAuthorizedEpic")

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        combined = result.stdout + result.stderr
        self.assertIn("epic-state-set-self", combined)
        self.assertIn("FINALIZER_REQUIRED", combined)
        orch = self.read_json(self.workspace / "state" / "orchestrator.json")
        self.assertEqual(orch["phase"], "executing")
        self.assertEqual(orch["authorizedEpic"], "epic-state-set-self")

    def test_state_set_ready_checks_approved_epic_finalizer_when_authorized_epic_absent(self) -> None:
        self.write_issue(
            "epic-approved-self",
            {
                "kind": "epic",
                "project": "(framework)",
                "state": "Human Review",
                "children": ["task-approved-child"],
                "finalizerRequired": True,
                "allowedPaths": ["scripts/state-set.sh"],
                "status": {
                    "finalizer": {
                        "ok": True,
                        "status": "passed",
                        "subjectCommitOid": "abc123",
                        "destinationRef": "refs/heads/main",
                        "changedPaths": ["scripts/state-set.sh"],
                        "stagedPaths": ["scripts/state-set.sh"],
                        "errors": [],
                    }
                },
            },
        )
        self.write_issue(
            "task-approved-child",
            {
                "kind": "task",
                "project": "(framework)",
                "parent": "epic-approved-self",
                "state": "Done",
                "finalizerNotRequired": True,
                "allowedPaths": ["docs/child.md"],
            },
        )
        self.write_json(
            self.workspace / "state" / "orchestrator.json",
            {
                "schemaVersion": 1,
                "activeProject": "(framework)",
                "phase": "executing",
                "authorizedEpic": None,
                "approvedEpicIds": ["epic-approved-self"],
                "approvedIssueIds": ["task-approved-child"],
                "maxConcurrentWorkers": 1,
                "runningIssues": [],
                "blockedIssues": [],
            },
        )

        result = self.exec_cmd("bash", str(self.workspace / "scripts" / "state-set.sh"), "--phase", "ready")

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        combined = result.stdout + result.stderr
        self.assertIn("epic-approved-self", combined)
        self.assertIn("FINALIZER_REQUIRED", combined)
        orch = self.read_json(self.workspace / "state" / "orchestrator.json")
        self.assertEqual(orch["phase"], "executing")
        self.assertIsNone(orch["authorizedEpic"])

    def test_ready_reconciliation_blocks_authorized_scope_missing_finalizer(self) -> None:
        self.write_issue(
            "epic-ready",
            {"kind": "epic", "project": "(framework)", "state": "Human Review", "children": ["task-ready"]},
        )
        self.write_issue(
            "task-ready",
            {
                "kind": "task",
                "project": "(framework)",
                "parent": "epic-ready",
                "state": "Done",
                "allowedPaths": ["scripts/orchestrator-tick.py"],
            },
        )
        self.write_json(
            self.workspace / "state" / "orchestrator.json",
            {
                "schemaVersion": 1,
                "activeProject": "(framework)",
                "phase": "executing",
                "authorizedEpic": "epic-ready",
                "maxConcurrentWorkers": 1,
                "runningIssues": [],
                "blockedIssues": [],
            },
        )

        result = self.exec_cmd("python3", str(self.workspace / "scripts" / "orchestrator-tick.py"), "--once")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        orch = self.read_json(self.workspace / "state" / "orchestrator.json")
        self.assertEqual(orch["phase"], "blocked")
        self.assertIn("FINALIZER_REQUIRED for READY reconciliation", orch["status"])
        self.assertEqual(orch["authorizedEpic"], "epic-ready")

    def test_strict_local_only_exception_requires_paths_rationale_and_renders(self) -> None:
        helper = load_module("finalizer_required_test_module", self.workspace / "scripts" / "finalizer_required.py")
        invalid_issue = {
            "id": "local-invalid",
            "project": "(framework)",
            "state": "Done",
            "allowedPaths": ["docs/local-note.md"],
            "status": {"finalizer": {"localOnly": {"paths": ["docs/local-note.md"]}}},
        }
        invalid = helper.finalizer_transition_guard(invalid_issue, "Done")
        self.assertFalse(invalid["ok"])
        self.assertIn("localOnly requires a rationale", invalid["reason"])

        vague_path_issue = {
            "id": "local-vague",
            "project": "(framework)",
            "state": "Done",
            "allowedPaths": ["docs/local-note.md"],
            "status": {
                "finalizer": {
                    "localOnly": {
                        "paths": ["docs/"],
                        "rationale": "too broad to be an exact local-only exception",
                    }
                }
            },
        }
        vague = helper.finalizer_transition_guard(vague_path_issue, "Done")
        self.assertFalse(vague["ok"])
        self.assertIn("path is not exact", vague["reason"])

        valid_issue = {
            "id": "local-valid",
            "project": "(framework)",
            "state": "Done",
            "allowedPaths": ["docs/local-note.md"],
            "status": {
                "finalizer": {
                    "localOnly": {
                        "paths": ["docs/local-note.md"],
                        "rationale": "documentation-only local note; no commit finalizer needed",
                        "completionMode": "local-only-human-review",
                        "doneStatus": "NOT_DONE",
                    }
                }
            },
        }
        valid = helper.finalizer_transition_guard(valid_issue, "Human Review")
        self.assertTrue(valid["ok"], valid)
        self.assertEqual(valid["status"], "localOnly")
        self.assertEqual(valid["doneStatus"], "NOT_DONE")

        ready = helper.finalizer_transition_guard(valid_issue, "READY reconciliation")
        self.assertFalse(ready["ok"], ready)
        self.assertEqual(ready["status"], "NOT_DONE")
        self.assertTrue(ready["readyForHuman"])

        not_done = helper.finalizer_transition_guard(valid_issue, "Done")
        self.assertFalse(not_done["ok"], not_done)
        self.assertEqual(not_done["status"], "NOT_DONE")
        self.assertTrue(not_done["readyForHuman"])

        render_state = load_module("render_state_finalizer_guard_test", self.workspace / "scripts" / "render-state.py")
        render_state.ROOT = self.workspace
        render_state.STATE_DIR = self.workspace / "state"
        line = render_state.issue_line(valid_issue)
        self.assertIn("finalizer=localOnly", line)
        self.assertIn("paths=docs/local-note.md", line)
        self.assertIn("rationale=documentation-only local note", line)
        self.assertIn("status=NOT_DONE", line)

        render_active = load_module("render_active_finalizer_guard_test", self.workspace / "scripts" / "render-active-tasks.py")
        task = render_active.render_task(valid_issue)
        self.assertEqual(task["finalizer"]["status"], "NOT_DONE")
        self.assertEqual(task["finalizer"]["localOnly"]["paths"], ["docs/local-note.md"])
        self.assertEqual(task["finalizer"]["localOnly"]["doneStatus"], "NOT_DONE")
        self.assertEqual(task["meta"]["finalizer"]["localOnly"]["rationale"], "documentation-only local note; no commit finalizer needed")


if __name__ == "__main__":
    unittest.main()
