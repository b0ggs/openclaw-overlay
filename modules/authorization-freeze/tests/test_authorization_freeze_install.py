#!/usr/bin/env python3
"""Installer tests for the authorization-freeze module."""

from __future__ import annotations

import json
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


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def write_render_stub(path: Path) -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


class AuthorizationFreezeInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.scripts_dir = self.workspace / "scripts"
        self.scripts_dir.mkdir()
        (self.workspace / "baseline.txt").write_text("baseline\n", encoding="utf-8")
        (self.scripts_dir / "freeze-authorization.py").write_text(
            "#!/usr/bin/env python3\n"
            "raise SystemExit('placeholder')\n",
            encoding="utf-8",
        )
        (self.scripts_dir / "freeze-authorization.py").chmod(0o755)
        write_render_stub(self.scripts_dir / "render-state.py")
        write_render_stub(self.scripts_dir / "render-active-tasks.py")
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

    def write_freeze_fixture(self) -> None:
        write_json(
            self.workspace / "state" / "issues" / "epic-a.json",
            {
                "id": "epic-a",
                "kind": "epic",
                "project": "proj",
                "state": "Todo",
                "children": ["issue-a"],
            },
        )
        write_json(
            self.workspace / "state" / "issues" / "issue-a.json",
            {
                "id": "issue-a",
                "kind": "task",
                "project": "proj",
                "parent": "epic-a",
                "state": "Todo",
            },
        )
        write_json(
            self.workspace / "state" / "orchestrator.json",
            {
                "schemaVersion": 1,
                "activeProject": "proj",
                "phase": "ready",
                "approvedEpicIds": [],
                "approvedIssueIds": [],
                "authorizationFrozenAt": None,
                "runningIssues": [],
                "blockedIssues": [],
            },
        )

    def test_install_is_idempotent_and_uninstall_leaves_no_trace(self) -> None:
        first = self.run_script("install.sh")
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        second = self.run_script("install.sh")
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)

        source = MODULE_DIR / "src" / "scripts" / "freeze-authorization.py"
        installed = self.scripts_dir / "freeze-authorization.py"
        self.assertEqual(source.read_bytes(), installed.read_bytes())

        removed = self.run_script("uninstall.sh")
        self.assertEqual(removed.returncode, 0, removed.stderr + removed.stdout)
        self.assertEqual(snapshot(self.workspace), self.before)

    def test_installed_cli_writes_frozen_authorization_snapshot(self) -> None:
        installed = self.run_script("install.sh")
        self.assertEqual(installed.returncode, 0, installed.stderr + installed.stdout)
        self.write_freeze_fixture()

        result = subprocess.run(
            ["python3", "scripts/freeze-authorization.py", "epic-a"],
            cwd=self.workspace,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        printed = json.loads(result.stdout)
        self.assertEqual(printed["project"], "proj")
        self.assertEqual(printed["approvedEpicIds"], ["epic-a"])
        self.assertEqual(printed["approvedIssueIds"], ["issue-a"])
        snapshot_payload = json.loads((self.workspace / "state" / "frozen-authorization.json").read_text())
        self.assertEqual(snapshot_payload["approvedEpicIds"], ["epic-a"])
        self.assertEqual(snapshot_payload["approvedIssueIds"], ["issue-a"])

    def test_uninstall_refuses_modified_installed_file(self) -> None:
        installed = self.run_script("install.sh")
        self.assertEqual(installed.returncode, 0, installed.stderr + installed.stdout)
        (self.scripts_dir / "freeze-authorization.py").write_text("changed\n", encoding="utf-8")

        removed = self.run_script("uninstall.sh")

        self.assertNotEqual(removed.returncode, 0)
        self.assertIn("target file changed after install", removed.stderr)


if __name__ == "__main__":
    unittest.main()
