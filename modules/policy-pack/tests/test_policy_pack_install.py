#!/usr/bin/env python3
from __future__ import annotations

import filecmp
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1]
OVERLAY_ROOT = MODULE_DIR.parents[1]


def snapshot(root: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(root))] = path.read_bytes()
    return result


class PolicyPackInstallTests(unittest.TestCase):
    def run_cmd(self, argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_install_is_idempotent_and_uninstall_leaves_no_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "workspace"
            target.mkdir()
            (target / "baseline.txt").write_text("baseline\n", encoding="utf-8")
            before = snapshot(target)

            install = MODULE_DIR / "install.sh"
            uninstall = MODULE_DIR / "uninstall.sh"

            first = self.run_cmd(["bash", str(install), str(target)], target)
            self.assertEqual(first.returncode, 0, first.stderr)
            second = self.run_cmd(["bash", str(install), str(target)], target)
            self.assertEqual(second.returncode, 0, second.stderr)

            for relpath in (
                "harness/path_config.py",
                "AGENTS.md",
                "WORKFLOW.md",
                "scripts/git-preflight.sh",
                "scripts/openclaw-runtime-preflight.py",
            ):
                source = MODULE_DIR / "src" / relpath
                if relpath == "harness/path_config.py":
                    source = OVERLAY_ROOT / "core" / relpath
                self.assertTrue(filecmp.cmp(source, target / relpath, shallow=False), relpath)

            removed = self.run_cmd(["bash", str(uninstall), str(target)], target)
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertEqual(before, snapshot(target))

    def test_installed_runtime_preflight_upstream_test_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "workspace"
            target.mkdir()
            (target / "scripts").mkdir()
            shutil.copy2(
                MODULE_DIR / "tests" / "upstream" / "scripts" / "test_openclaw_runtime_preflight.py",
                target / "scripts" / "test_openclaw_runtime_preflight.py",
            )

            installed = self.run_cmd(["bash", str(MODULE_DIR / "install.sh"), str(target)], target)
            self.assertEqual(installed.returncode, 0, installed.stderr)

            result = self.run_cmd(
                ["python3", "-m", "unittest", "discover", "-s", "scripts", "-p", "test_openclaw_runtime_preflight.py"],
                target,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_installed_git_preflight_blocks_secret_env_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "workspace"
            target.mkdir()
            subprocess.run(["git", "init"], cwd=target, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=target, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=target, check=True)
            (target / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
            subprocess.run(["git", "add", ".gitignore"], cwd=target, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=target, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            installed = self.run_cmd(["bash", str(MODULE_DIR / "install.sh"), str(target)], target)
            self.assertEqual(installed.returncode, 0, installed.stderr)

            (target / ".env").write_text("TOKEN=example\n", encoding="utf-8")
            result = self.run_cmd(["bash", "scripts/git-preflight.sh", ".", "--paths", ".env"], target)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("declared paths look secret-prone", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
