#!/usr/bin/env python3
"""Installer tests for the execution-pattern-router module."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1]
OVERLAY_ROOT = MODULE_DIR.parents[1]


def snapshot(root: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest = subprocess.check_output(["sha256sum", str(path)], text=True).split()[0]
            rows.append((path.relative_to(root).as_posix(), digest))
    return rows


class ExecutionPatternRouterInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.scripts_dir = self.workspace / "scripts"
        self.harness_dir = self.workspace / "harness"
        self.scripts_dir.mkdir()
        self.harness_dir.mkdir()
        (self.workspace / "baseline.txt").write_text("baseline\n", encoding="utf-8")
        (self.harness_dir / "__init__.py").write_text("", encoding="utf-8")
        shutil.copy2(OVERLAY_ROOT / "core" / "harness" / "path_config.py", self.harness_dir / "path_config.py")
        (self.scripts_dir / "execution_pattern_router.py").write_text(
            "raise ImportError('execution-pattern-router overlay module is not installed')\n",
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

    def test_install_is_idempotent_and_uninstall_restores_placeholder(self) -> None:
        first = self.run_script("install.sh")
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        second = self.run_script("install.sh")
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)

        source = MODULE_DIR / "src" / "scripts" / "execution_pattern_router.py"
        installed = self.scripts_dir / "execution_pattern_router.py"
        self.assertEqual(source.read_bytes(), installed.read_bytes())

        env = {
            **os.environ,
            "OPENCLAW_WORKSPACE_ROOT": str(self.workspace),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        result = subprocess.run(
            [
                "python3",
                "-c",
                "from scripts.execution_pattern_router import canonicalize_execution_pattern; "
                "print(canonicalize_execution_pattern('bridge_critic'))",
            ],
            cwd=self.workspace,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(result.stdout.strip(), "critic")

        removed = self.run_script("uninstall.sh")
        self.assertEqual(removed.returncode, 0, removed.stderr + removed.stdout)
        self.assertEqual(snapshot(self.workspace), self.before)

    def test_uninstall_refuses_modified_installed_file(self) -> None:
        installed = self.run_script("install.sh")
        self.assertEqual(installed.returncode, 0, installed.stderr + installed.stdout)
        (self.scripts_dir / "execution_pattern_router.py").write_text("changed\n", encoding="utf-8")

        removed = self.run_script("uninstall.sh")

        self.assertNotEqual(removed.returncode, 0)
        self.assertIn("target file changed after install", removed.stderr)


if __name__ == "__main__":
    unittest.main()
