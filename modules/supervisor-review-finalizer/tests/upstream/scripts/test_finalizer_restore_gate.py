#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FINALIZER = REPO_ROOT / "scripts" / "finalizer.py"
CHANGE_SCOPE = REPO_ROOT / "scripts" / "restore-change-scope-check.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


change_scope = load_module("restore_change_scope_check", CHANGE_SCOPE)


class FinalizerRestoreGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.repo = Path(self.tempdir.name) / "repo"
        self.issue_id = "fwk-chh-w4-finalizer-restore-gate-20260514"
        self.dest_ref = "refs/heads/continuous-harness-hygiene-20260514"

        self.git("init", str(self.repo), repo=None)
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test User")
        self.write("docs/notes.md", "docs baseline\n")
        self.write("scripts/finalizer.py", "finalizer baseline\n")
        self.write(
            "scripts/restore-completeness-check.py",
            "import json\nprint(json.dumps({'passed': True, 'checker': 'scripts/restore-completeness-check.py'}))\n",
        )
        self.write("WORKFLOW.md", "workflow baseline\n")
        self.write(".github/workflows/ci.yml", "name: ci\n")
        self.write("status/finalizer.json", '{"status":"baseline"}\n')
        self.write("evidence/restore-pass.json", json.dumps({"passed": True, "checker": "scripts/restore-completeness-check.py"}) + "\n")
        self.write("evidence/restore-fail.json", json.dumps({"passed": False, "checker": "scripts/restore-completeness-check.py"}) + "\n")
        self.git(
            "add",
            "--",
            "docs/notes.md",
            "scripts/finalizer.py",
            "scripts/restore-completeness-check.py",
            "WORKFLOW.md",
            ".github/workflows/ci.yml",
            "status/finalizer.json",
            "evidence/restore-pass.json",
            "evidence/restore-fail.json",
        )
        self.git("commit", "-m", "init")
        self.base_commit = self.git("rev-parse", "HEAD").stdout.strip()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write(self, relative: str, content: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def run_cmd(
        self,
        cmd: list[str],
        *,
        repo: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "HOME": str(self.home),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if repo is not None:
            cmd = [cmd[0], "-C", str(repo), *cmd[1:]]
        return subprocess.run(cmd, env=env, text=True, capture_output=True, check=check)

    def git(
        self,
        *args: str,
        repo: Path | None = "default",  # type: ignore[assignment]
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        git_repo = self.repo if repo == "default" else repo
        return self.run_cmd(["git", *args], repo=git_repo, check=check)

    def run_finalizer(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        cmd = [
            sys.executable,
            str(FINALIZER),
            "--repo",
            str(self.repo),
            "--issue-id",
            self.issue_id,
            "--base-commit",
            self.base_commit,
            "--dest-ref",
            self.dest_ref,
            *args,
        ]
        return self.run_cmd(cmd, check=check)

    def payload(self, result: subprocess.CompletedProcess[str]) -> dict[str, object]:
        self.assertTrue(result.stdout.strip(), result.stderr)
        return json.loads(result.stdout)

    def cached_paths(self) -> list[str]:
        output = self.git("diff", "--cached", "--name-only", "-z", "--").stdout
        return sorted(path for path in output.split("\0") if path)

    def test_scripts_and_root_workflow_docs_require_restore_gate(self) -> None:
        self.write("scripts/finalizer.py", "finalizer changed\n")
        self.write("WORKFLOW.md", "workflow changed\n")

        result = self.run_finalizer("--path", "scripts/finalizer.py", "--path", "WORKFLOW.md")

        self.assertNotEqual(result.returncode, 0)
        payload = self.payload(result)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["errors"], [{"kind": "restore_gate_required", "message": "restore/change-scope gate required for harness-affecting paths"}])
        gate = payload["restoreGate"]
        self.assertEqual(gate["status"], "required_missing")
        self.assertEqual(gate["changeScope"]["reasonCodes"], ["root_workflow_doc", "script"])
        self.assertEqual(self.cached_paths(), [])

    def test_docs_only_safe_path_does_not_require_restore_gate(self) -> None:
        self.write("docs/notes.md", "docs changed\n")

        result = self.run_finalizer("--path", "docs/notes.md")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = self.payload(result)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["restoreGate"]["required"])
        self.assertEqual(payload["restoreGate"]["status"], "not_required")
        self.assertEqual(payload["restoreGate"]["changeScope"]["safePaths"], ["docs/notes.md"])
        self.assertEqual(self.cached_paths(), [])

    def test_restore_evidence_pass_allows_dry_run_metadata_ok(self) -> None:
        self.write("scripts/finalizer.py", "finalizer changed\n")

        result = self.run_finalizer(
            "--path",
            "scripts/finalizer.py",
            "--restore-evidence-file",
            "evidence/restore-pass.json",
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = self.payload(result)
        self.assertTrue(payload["ok"])
        gate = payload["restoreGate"]
        self.assertTrue(gate["required"])
        self.assertEqual(gate["status"], "passed")
        self.assertEqual(gate["evidence"]["status"], "passed")
        self.assertTrue(gate["evidence"]["metadataOnly"])
        self.assertIn("subjectCommitOid", gate["evidence"])
        self.assertEqual(self.cached_paths(), [])

    def test_run_repo_local_restore_checker_passes_when_requested(self) -> None:
        self.write("scripts/finalizer.py", "finalizer changed\n")

        result = self.run_finalizer(
            "--path",
            "scripts/finalizer.py",
            "--run-restore-check",
            "--restore-check-skip-git-status",
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = self.payload(result)
        self.assertTrue(payload["ok"])
        evidence = payload["restoreGate"]["evidence"]
        self.assertEqual(evidence["mode"], "restore-completeness-check")
        self.assertEqual(evidence["status"], "passed")
        self.assertFalse(evidence["liveMutation"])
        self.assertFalse(evidence["rawLogsIncluded"])
        self.assertEqual(self.cached_paths(), [])

    def test_restore_evidence_fail_blocks(self) -> None:
        self.write("scripts/finalizer.py", "finalizer changed\n")

        result = self.run_finalizer(
            "--path",
            "scripts/finalizer.py",
            "--restore-evidence-file",
            "evidence/restore-fail.json",
        )

        self.assertNotEqual(result.returncode, 0)
        payload = self.payload(result)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["restoreGate"]["status"], "failed")
        self.assertIn({"kind": "restore_gate_failed", "message": "restore/change-scope evidence did not pass"}, payload["errors"])
        self.assertEqual(self.cached_paths(), [])

    def test_official_rehearsal_not_done_or_blocked_evidence_blocks_overclaim(self) -> None:
        cases = {
            "not-done": {"status": "NOT_DONE", "doneStatus": "NOT_DONE", "blockerCode": "official_install_rehearsal_not_performed"},
            "blocked": {"status": "BLOCKED", "doneStatus": "BLOCKED", "blockerCode": "missing_github_read_credentials"},
        }
        for label, evidence in cases.items():
            with self.subTest(label=label):
                self.write("scripts/finalizer.py", "finalizer changed\n")
                self.write(f"evidence/restore-{label}.json", json.dumps(evidence) + "\n")

                result = self.run_finalizer(
                    "--path",
                    "scripts/finalizer.py",
                    "--restore-evidence-file",
                    f"evidence/restore-{label}.json",
                )

                self.assertNotEqual(result.returncode, 0)
                payload = self.payload(result)
                self.assertFalse(payload["ok"])
                self.assertIn({"kind": "restore_gate_failed", "message": "restore/change-scope evidence did not pass"}, payload["errors"])
                self.assertEqual(self.cached_paths(), [])

    def test_planned_workflow_status_and_evidence_paths_are_classified(self) -> None:
        scope = change_scope.build_scope(
            changed_paths=[
                ".github/workflows/restore.yml",
                "status/finalizer.json",
                "evidence/restore-evidence.json",
            ],
            manifest_paths=[
                ".github/workflows/restore.yml",
                "status/finalizer.json",
                "evidence/restore-evidence.json",
            ],
        )

        self.assertTrue(scope["restoreRequired"])
        self.assertEqual(scope["harnessAffectingPaths"], [
            ".github/workflows/restore.yml",
            "evidence/restore-evidence.json",
            "status/finalizer.json",
        ])
        self.assertEqual(scope["reasonCodes"], ["evidence_metadata", "github_workflow", "status_metadata"])

    def test_change_scope_rejects_traversal_for_sensitive_path_classes(self) -> None:
        traversal_paths = [
            "docs/../scripts/finalizer.py",
            "docs/../.github/workflows/restore.yml",
            "docs/../status/finalizer.json",
            "docs/../evidence/restore-evidence.json",
            "docs/../mds/check.yml",
        ]
        for traversal_path in traversal_paths:
            with self.subTest(traversal_path=traversal_path):
                with self.assertRaises(ValueError):
                    change_scope.build_scope(changed_paths=[traversal_path], manifest_paths=[traversal_path])

    def test_restore_evidence_nested_failed_status_blocks_even_with_top_level_passed(self) -> None:
        self.write("scripts/finalizer.py", "finalizer changed\n")
        self.write(
            "evidence/restore-overclaim.json",
            json.dumps({"passed": True, "checks": [{"name": "restore", "status": "failed"}]}) + "\n",
        )

        result = self.run_finalizer(
            "--path",
            "scripts/finalizer.py",
            "--restore-evidence-file",
            "evidence/restore-overclaim.json",
        )

        self.assertNotEqual(result.returncode, 0)
        payload = self.payload(result)
        evidence = payload["restoreGate"]["evidence"]
        self.assertEqual(evidence["status"], "failed")
        self.assertEqual(evidence["failedCheckCount"], 1)
        self.assertIn({"kind": "restore_gate_failed", "message": "restore/change-scope evidence did not pass"}, payload["errors"])

    def test_restore_evidence_non_empty_errors_block_even_with_passed_true(self) -> None:
        self.write("scripts/finalizer.py", "finalizer changed\n")
        self.write(
            "evidence/restore-errors.json",
            json.dumps({"passed": True, "errors": [{"kind": "restore_failed"}]}) + "\n",
        )

        result = self.run_finalizer(
            "--path",
            "scripts/finalizer.py",
            "--restore-evidence-file",
            "evidence/restore-errors.json",
        )

        self.assertNotEqual(result.returncode, 0)
        payload = self.payload(result)
        evidence = payload["restoreGate"]["evidence"]
        self.assertEqual(evidence["status"], "failed")
        self.assertEqual(evidence["errorCount"], 1)

    def test_restore_evidence_forbidden_metadata_keys_block_without_value_leak(self) -> None:
        self.write("scripts/finalizer.py", "finalizer changed\n")
        sentinel = "RAW_LOG_SENTINEL_123"
        self.write(
            "evidence/restore-raw.json",
            json.dumps(
                {
                    "passed": True,
                    "rawLogText": sentinel,
                    "logOutput": sentinel,
                    "environmentVariables": {"TOKEN": sentinel},
                    "credentialMaterial": sentinel,
                    "env": sentinel,
                    "ENV": sentinel,
                }
            ) + "\n",
        )

        result = self.run_finalizer(
            "--path",
            "scripts/finalizer.py",
            "--restore-evidence-file",
            "evidence/restore-raw.json",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(sentinel, result.stdout)
        self.assertNotIn(sentinel, result.stderr)
        payload = self.payload(result)
        evidence = payload["restoreGate"]["evidence"]
        self.assertEqual(evidence["status"], "invalid")
        self.assertIn("rawLogText", evidence["forbiddenEvidenceKeys"])
        self.assertIn("env", evidence["forbiddenEvidenceKeys"])
        self.assertIn("ENV", evidence["forbiddenEvidenceKeys"])

    def test_restore_evidence_malformed_errors_field_blocks(self) -> None:
        self.write("scripts/finalizer.py", "finalizer changed\n")
        self.write("evidence/restore-malformed-errors.json", json.dumps({"passed": True, "errors": "not-a-list"}) + "\n")

        result = self.run_finalizer(
            "--path",
            "scripts/finalizer.py",
            "--restore-evidence-file",
            "evidence/restore-malformed-errors.json",
        )

        self.assertNotEqual(result.returncode, 0)
        payload = self.payload(result)
        evidence = payload["restoreGate"]["evidence"]
        self.assertEqual(evidence["status"], "failed")
        self.assertEqual(evidence["errorFieldCount"], 1)

    def test_restore_evidence_nested_check_errors_block(self) -> None:
        self.write("scripts/finalizer.py", "finalizer changed\n")
        self.write(
            "evidence/restore-nested-errors.json",
            json.dumps({"passed": True, "checks": [{"name": "restore", "passed": True, "errors": [{"kind": "nested"}]}]}) + "\n",
        )

        result = self.run_finalizer(
            "--path",
            "scripts/finalizer.py",
            "--restore-evidence-file",
            "evidence/restore-nested-errors.json",
        )

        self.assertNotEqual(result.returncode, 0)
        payload = self.payload(result)
        evidence = payload["restoreGate"]["evidence"]
        self.assertEqual(evidence["status"], "failed")
        self.assertEqual(evidence["failedCheckCount"], 1)

    def test_restore_evidence_invalid_subject_commit_blocks_without_echo(self) -> None:
        self.write("scripts/finalizer.py", "finalizer changed\n")
        invalid_subject = "not-a-commit-oid"
        self.write(
            "evidence/restore-invalid-subject.json",
            json.dumps({"passed": True, "subjectCommitOid": invalid_subject}) + "\n",
        )

        result = self.run_finalizer(
            "--path",
            "scripts/finalizer.py",
            "--restore-evidence-file",
            "evidence/restore-invalid-subject.json",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(invalid_subject, result.stdout)
        self.assertNotIn(invalid_subject, result.stderr)
        payload = self.payload(result)
        evidence = payload["restoreGate"]["evidence"]
        self.assertEqual(evidence["status"], "invalid")
        self.assertEqual(evidence["errors"][0]["kind"], "restore_evidence_invalid_subject_commit")
        self.assertNotIn("artifactSubjectCommitOid", evidence)

    def test_restore_evidence_does_not_echo_artifact_script_strings(self) -> None:
        freeform = "FREEFORM_SCRIPT_STRING_SHOULD_NOT_ECHO"
        self.write(
            "evidence/restore-script-string.json",
            json.dumps({"passed": True, "script": freeform, "checker": freeform}) + "\n",
        )
        self.git("add", "--", "evidence/restore-script-string.json")
        self.git("commit", "-m", "add script string evidence")
        self.base_commit = self.git("rev-parse", "HEAD").stdout.strip()
        self.write("scripts/finalizer.py", "finalizer changed\n")

        result = self.run_finalizer(
            "--path",
            "scripts/finalizer.py",
            "--restore-evidence-file",
            "evidence/restore-script-string.json",
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(freeform, result.stdout)
        self.assertNotIn(freeform, result.stderr)
        payload = self.payload(result)
        evidence = payload["restoreGate"]["evidence"]
        self.assertTrue(evidence["artifactCheckerProvided"])
        self.assertNotIn("script", evidence)
        self.assertNotIn("checker", evidence)

    def test_stage_mode_with_restore_gate_stages_exact_manifest_path_only(self) -> None:
        self.write("scripts/finalizer.py", "finalizer changed\n")

        result = self.run_finalizer(
            "--stage",
            "--path",
            "scripts/finalizer.py",
            "--restore-evidence-file",
            "evidence/restore-pass.json",
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = self.payload(result)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["stagedPaths"], ["scripts/finalizer.py"])
        self.assertEqual(self.cached_paths(), ["scripts/finalizer.py"])
        self.assertFalse(payload["restoreGate"]["policy"]["liveRuntimeMutation"])
        self.assertFalse(payload["restoreGate"]["policy"]["rawDiffIncluded"])


if __name__ == "__main__":
    unittest.main()
