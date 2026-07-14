#!/usr/bin/env python3
"""Installer tests for the list-bound execution gate module."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1]


def snapshot(root: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest = subprocess.check_output(["sha256sum", str(path)], text=True).split()[0]
            rows.append((path.relative_to(root).as_posix(), digest))
    return rows


class ListBoundExecutionGateInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.scripts_dir = self.workspace / "scripts"
        self.scripts_dir.mkdir()
        (self.workspace / "baseline.txt").write_text("baseline\n", encoding="utf-8")
        (self.scripts_dir / "openclaw-paths.sh").write_text(
            'OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"\n'
            'OPENCLAW_WORKSPACE_ROOT="${OPENCLAW_WORKSPACE_ROOT:-$OPENCLAW_HOME/workspace}"\n'
            'OPENCLAW_STOP_FILE="${OPENCLAW_STOP_FILE:-$OPENCLAW_HOME/STOP}"\n',
            encoding="utf-8",
        )
        state_dir = self.workspace / "state" / "issues"
        state_dir.mkdir(parents=True)
        (self.workspace / "state" / "orchestrator.json").write_text(
            json.dumps(
                {
                    "authorizedEpic": "epic-a",
                    "authorizationFrozenAt": None,
                    "approvedIssueIds": [],
                    "approvedEpicIds": [],
                }
            ),
            encoding="utf-8",
        )
        (state_dir / "epic-a.json").write_text(
            json.dumps({"id": "epic-a", "kind": "epic", "children": ["issue-a"]}),
            encoding="utf-8",
        )
        (state_dir / "issue-a.json").write_text(
            json.dumps({"id": "issue-a", "kind": "task", "state": "Todo", "parent": "epic-a"}),
            encoding="utf-8",
        )
        self.before = snapshot(self.workspace)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def run_script(self, name: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(MODULE_DIR / name), str(self.workspace)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_install_is_idempotent_and_uninstall_leaves_no_trace(self) -> None:
        first = self.run_script("install.sh")
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        second = self.run_script("install.sh")
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)

        env = {
            **os.environ,
            "OPENCLAW_WORKSPACE_ROOT": str(self.workspace),
        }
        env.pop("OPENCLAW_OVERLAY_V2_ROOT", None)
        decision = subprocess.run(
            ["bash", str(self.scripts_dir / "list-bound-execution-gate-check.sh"), "issue-a", "dispatch"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(decision.returncode, 0, decision.stderr + decision.stdout)
        self.assertEqual(json.loads(decision.stdout)["result"], "ALLOW")

        removed = self.run_script("uninstall.sh")
        self.assertEqual(removed.returncode, 0, removed.stderr + removed.stdout)
        self.assertEqual(snapshot(self.workspace), self.before)

    def test_uninstall_refuses_modified_installed_file(self) -> None:
        installed = self.run_script("install.sh")
        self.assertEqual(installed.returncode, 0, installed.stderr + installed.stdout)
        (self.scripts_dir / "list-bound-execution-gate-check.sh").write_text("changed\n", encoding="utf-8")

        removed = self.run_script("uninstall.sh")

        self.assertNotEqual(removed.returncode, 0)
        self.assertIn("target file changed after install", removed.stderr)


if __name__ == "__main__":
    unittest.main()
