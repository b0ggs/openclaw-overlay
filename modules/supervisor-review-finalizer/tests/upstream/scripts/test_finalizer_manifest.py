#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import finalizer  # noqa: E402

FINALIZER = REPO_ROOT / "scripts" / "finalizer.py"


class FinalizerManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.repo = Path(self.tempdir.name) / "repo"
        self.issue_id = "fwk-chh-w2-finalizer-manifest-20260514"
        self.dest_ref = "refs/heads/continuous-harness-hygiene-20260514"

        self.git("init", str(self.repo), repo=None)
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test User")
        (self.repo / "docs").mkdir(parents=True, exist_ok=True)
        (self.repo / "docs" / "allowed.md").write_text("allowed baseline\n")
        (self.repo / "rogue.md").write_text("rogue baseline\n")
        self.git("add", "--", "docs/allowed.md", "rogue.md")
        self.git("commit", "-m", "init")
        self.base_commit = self.git("rev-parse", "HEAD").stdout.strip()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

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

    def test_clean_manifest_dry_run_passes_without_staging(self) -> None:
        (self.repo / "docs" / "allowed.md").write_text("allowed change\n")

        result = self.run_finalizer("--path", "docs/allowed.md")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = self.payload(result)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["manifestPaths"], ["docs/allowed.md"])
        self.assertEqual(payload["changedPaths"], ["docs/allowed.md"])
        self.assertEqual(payload["wouldStagePaths"], ["docs/allowed.md"])
        section4 = payload["section4"]
        self.assertTrue(section4["metadataOnly"])
        self.assertEqual(section4["redaction"]["sensitiveValues"], "excluded")
        self.assertEqual(section4["redaction"]["consoleText"], "excluded")
        self.assertEqual(section4["redaction"]["changeContent"], "excluded")
        self.assertEqual(section4["doneStatus"], "NOT_DONE")
        self.assertEqual(section4["pathScope"]["approvedPaths"], ["docs/allowed.md"])
        self.assertEqual(section4["destination"]["destinationRef"], self.dest_ref)
        self.assertEqual(section4["accessLane"]["valueExposure"], False)
        forbidden_key_fragments = ("raw", "secret", "credential", "diff", "log", "patch", "token")

        def walk_keys(value: object) -> list[str]:
            if isinstance(value, dict):
                keys = list(value)
                for child in value.values():
                    keys.extend(walk_keys(child))
                return keys
            if isinstance(value, list):
                keys: list[str] = []
                for child in value:
                    keys.extend(walk_keys(child))
                return keys
            return []

        for key in walk_keys(section4):
            normalized = key.lower()
            for fragment in forbidden_key_fragments:
                with self.subTest(key=key, fragment=fragment):
                    self.assertNotIn(fragment, normalized)
        self.assertEqual(self.cached_paths(), [])


    def test_finalizer_scope_includes_committed_branch_paths_since_base(self) -> None:
        (self.repo / "docs" / "committed.md").write_text("committed change\n")
        self.git("add", "--", "docs/committed.md")
        self.git("commit", "-m", "branch committed change")
        (self.repo / "docs" / "allowed.md").write_text("allowed change\n")

        result = self.run_finalizer(
            "--path",
            "docs/allowed.md",
            "--path",
            "docs/committed.md",
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = self.payload(result)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["changedPaths"], ["docs/allowed.md", "docs/committed.md"])
        self.assertEqual(payload["wouldStagePaths"], ["docs/allowed.md", "docs/committed.md"])

    def test_unrelated_dirty_tracked_file_blocks(self) -> None:
        (self.repo / "docs" / "allowed.md").write_text("allowed change\n")
        (self.repo / "rogue.md").write_text("rogue change\n")

        result = self.run_finalizer("--path", "docs/allowed.md")

        self.assertNotEqual(result.returncode, 0)
        payload = self.payload(result)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["unrelatedDirtyPaths"], ["rogue.md"])
        self.assertEqual(payload["errors"], [{"kind": "unrelated_dirty_path", "path": "rogue.md"}])
        self.assertEqual(self.cached_paths(), [])

    def test_unrelated_untracked_file_blocks(self) -> None:
        (self.repo / "docs" / "allowed.md").write_text("allowed change\n")
        (self.repo / "loose.txt").write_text("untracked\n")

        result = self.run_finalizer("--path", "docs/allowed.md")

        self.assertNotEqual(result.returncode, 0)
        payload = self.payload(result)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["unrelatedDirtyPaths"], ["loose.txt"])
        self.assertEqual(self.cached_paths(), [])

    def test_manifest_path_escape_blocks(self) -> None:
        result = self.run_finalizer("--path", "../outside.txt")

        self.assertNotEqual(result.returncode, 0)
        payload = self.payload(result)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["errors"][0]["kind"], "manifest_path_escape")

    def test_manifest_file_escape_blocks_without_leaking_contents(self) -> None:
        outside = Path(self.tempdir.name) / "outside-manifest.txt"
        sentinel = "OUTSIDE_MANIFEST_SENTINEL_123"
        outside.write_text(f"{sentinel}\n")

        result = self.run_finalizer("--manifest-file", str(outside))

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(sentinel, result.stdout)
        self.assertNotIn(sentinel, result.stderr)
        payload = self.payload(result)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["errors"][0]["kind"], "manifest_path_escape")

    def test_repo_local_manifest_file_is_supported(self) -> None:
        manifest = self.repo / "docs" / "manifest.txt"
        manifest.write_text("docs/allowed.md\n")
        self.git("add", "--", "docs/manifest.txt")
        self.git("commit", "-m", "add manifest")
        self.base_commit = self.git("rev-parse", "HEAD").stdout.strip()
        (self.repo / "docs" / "allowed.md").write_text("allowed change\n")

        result = self.run_finalizer("--manifest-file", "docs/manifest.txt")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = self.payload(result)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["manifestPaths"], ["docs/allowed.md"])
        self.assertEqual(payload["changedPaths"], ["docs/allowed.md"])

    def test_stage_helper_uses_exact_literal_pathspecs(self) -> None:
        calls: list[list[str]] = []

        def fake_git(repo_root: Path, args: list[str] | tuple[str, ...]) -> str:
            calls.append(list(args))
            return ""

        finalizer.stage_manifest_paths(
            self.repo,
            ["docs/allowed.md", "literal*.txt"],
            git_runner=fake_git,
        )

        self.assertEqual(
            calls,
            [["add", "-f", "--", ":(literal)docs/allowed.md", ":(literal)literal*.txt"]],
        )
        self.assertNotIn("-A", calls[0])
        self.assertNotEqual(calls[0][-1], ".")


    def test_stage_mode_force_adds_explicit_ignored_manifest_path(self) -> None:
        info_exclude = self.repo / ".git" / "info" / "exclude"
        info_exclude.write_text(info_exclude.read_text() + "\ndocs/reports/\n", encoding="utf-8")
        ignored_report = self.repo / "docs" / "reports" / "final.md"
        ignored_report.parent.mkdir(parents=True, exist_ok=True)
        ignored_report.write_text("final report\n", encoding="utf-8")

        result = self.run_finalizer("--stage", "--path", "docs/reports/final.md")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = self.payload(result)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["changedPaths"], ["docs/reports/final.md"])
        self.assertEqual(payload["stagedPaths"], ["docs/reports/final.md"])
        self.assertEqual(self.cached_paths(), ["docs/reports/final.md"])

    def test_stage_mode_can_restage_explicit_force_added_ignored_manifest_path(self) -> None:
        info_exclude = self.repo / ".git" / "info" / "exclude"
        info_exclude.write_text(info_exclude.read_text() + "\ndocs/reports/\n", encoding="utf-8")
        ignored_report = self.repo / "docs" / "reports" / "final.md"
        ignored_report.parent.mkdir(parents=True, exist_ok=True)
        ignored_report.write_text("final report\n", encoding="utf-8")
        self.git("add", "-f", "--", "docs/reports/final.md")

        result = self.run_finalizer("--stage", "--path", "docs/reports/final.md")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = self.payload(result)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["stagedPaths"], ["docs/reports/final.md"])
        self.assertEqual(self.cached_paths(), ["docs/reports/final.md"])

    def test_stage_mode_stages_manifest_paths_only(self) -> None:
        (self.repo / "docs" / "allowed.md").write_text("allowed change\n")
        (self.repo / "new-file.txt").write_text("new manifest file\n")

        result = self.run_finalizer(
            "--stage",
            "--path",
            "docs/allowed.md",
            "--path",
            "new-file.txt",
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = self.payload(result)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["stagedPaths"], ["docs/allowed.md", "new-file.txt"])
        self.assertEqual(payload["section4"]["pathScope"]["stagedOrCommittedPaths"], ["docs/allowed.md", "new-file.txt"])
        self.assertEqual(self.cached_paths(), ["docs/allowed.md", "new-file.txt"])

    def test_output_does_not_contain_raw_diff_content(self) -> None:
        sentinel = "RAW_DIFF_SENTINEL_123"
        (self.repo / "docs" / "allowed.md").write_text(f"{sentinel}\n")

        result = self.run_finalizer("--path", "docs/allowed.md")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(sentinel, result.stdout)
        self.assertNotIn(sentinel, result.stderr)
        payload = self.payload(result)
        self.assertEqual(payload["changedPaths"], ["docs/allowed.md"])

    def test_section4_remote_metadata_redacts_token_shaped_origin_url(self) -> None:
        sentinel = "TOKEN_VALUE_SHOULD_NOT_LEAK_123"
        remote = "https://" + "x-access-token" + ":" + sentinel + "@github.com/b0ggs/openclaw-harness.git"
        self.git("remote", "add", "origin", remote)
        (self.repo / "docs" / "allowed.md").write_text("allowed change\n")

        result = self.run_finalizer("--path", "docs/allowed.md")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(sentinel, result.stdout)
        self.assertNotIn(sentinel, result.stderr)
        payload = self.payload(result)
        encoded_section4 = json.dumps(payload["section4"], sort_keys=True)
        self.assertNotIn(sentinel, encoded_section4)
        section4_repo = payload["section4"]["repo"]
        self.assertNotIn("canonicalRemoteUrl", section4_repo)
        self.assertNotIn("observedRemoteUrl", section4_repo)
        self.assertEqual(
            section4_repo["remote"],
            {
                "metadataOnly": True,
                "valueExposure": False,
                "present": True,
                "protocol": "https",
                "host": "github.com",
                "owner": "b0ggs",
                "repo": "openclaw-harness",
                "userinfoPresent": True,
                "parseable": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
