#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1]
OVERLAY_ROOT = MODULE_DIR.parents[1]


def snapshot(root: Path) -> dict[str, tuple[str, bytes | None, int | None]]:
    result: dict[str, tuple[str, bytes | None, int | None]] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        mode = stat.S_IMODE(path.stat().st_mode)
        if path.is_file():
            result[rel] = ("file", path.read_bytes(), mode)
        elif path.is_dir():
            result[rel] = ("dir", None, mode)
    return result


class PromptPackInstallTests(unittest.TestCase):
    def run_cmd(
        self,
        argv: list[str],
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        return subprocess.run(
            argv,
            cwd=cwd,
            env=full_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def render_env(self, target: Path) -> dict[str, str]:
        home = target / "openclaw-home"
        return {
            "OPENCLAW_HOME": str(home),
            "OPENCLAW_WORKSPACE_ROOT": str(target),
            "OPENCLAW_MDS_ROOT": str(home / "repos" / "openclaw-mds"),
            "OPENCLAW_PROJECTS_ROOT": str(home / "projects"),
            "OPENCLAW_EXTERNAL_PROJECTS_ROOT": str(target / "external-projects"),
            "OPENCLAW_WORKTREES_ROOT": str(home / "worktrees"),
            "OPENCLAW_STOP_FILE": str(home / "STOP"),
            "OPENCLAW_RUNS_ROOT": str(target / "runs"),
        }

    def test_install_is_idempotent_and_uninstall_leaves_no_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "workspace"
            target.mkdir()
            (target / "baseline.txt").write_text("baseline\n", encoding="utf-8")
            before = snapshot(target)
            env = self.render_env(target)

            install = MODULE_DIR / "install.sh"
            uninstall = MODULE_DIR / "uninstall.sh"

            first = self.run_cmd(["bash", str(install), str(target)], target, env)
            self.assertEqual(first.returncode, 0, first.stderr)
            second = self.run_cmd(["bash", str(install), str(target)], target, env)
            self.assertEqual(second.returncode, 0, second.stderr)

            boot_text = (target / "BOOT.md").read_text(encoding="utf-8")
            self.assertNotIn("${OPENCLAW_WORKSPACE_ROOT}", boot_text)
            self.assertIn(str(target), boot_text)
            self.assertTrue(os.access(target / "scripts" / "render-boot-index.py", os.X_OK))
            self.assertTrue(os.access(target / "verify" / "run-sam-canary.sh", os.X_OK))
            self.assertTrue((target / "scripts" / "test_sam_canary.py").is_file())

            removed = self.run_cmd(["bash", str(uninstall), str(target)], target, env)
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertEqual(before, snapshot(target))

    def test_uninstall_refuses_changed_installed_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "workspace"
            target.mkdir()
            env = self.render_env(target)

            installed = self.run_cmd(["bash", str(MODULE_DIR / "install.sh"), str(target)], target, env)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            (target / "BOOT.md").write_text("changed\n", encoding="utf-8")

            result = self.run_cmd(["bash", str(MODULE_DIR / "uninstall.sh"), str(target)], target, env)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("target file changed after install", result.stderr)

    def test_reinstall_adds_files_missing_from_older_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "workspace"
            target.mkdir()
            (target / "baseline.txt").write_text("baseline\n", encoding="utf-8")
            before = snapshot(target)
            env = self.render_env(target)

            install = MODULE_DIR / "install.sh"
            uninstall = MODULE_DIR / "uninstall.sh"
            first = self.run_cmd(["bash", str(install), str(target)], target, env)
            self.assertEqual(first.returncode, 0, first.stderr)

            new_paths = {"scripts/test_sam_canary.py", "verify/run-sam-canary.sh"}
            manifest = target / ".openclaw-overlay" / "modules" / "prompt-pack" / "manifest.tsv"
            manifest.write_text(
                "".join(
                    line for line in manifest.read_text(encoding="utf-8").splitlines(keepends=True)
                    if line.split("\t", 1)[0] not in new_paths
                ),
                encoding="utf-8",
            )
            for rel in new_paths:
                (target / rel).unlink()
            (target / "verify").rmdir()

            upgraded = self.run_cmd(["bash", str(install), str(target)], target, env)
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
            self.assertTrue((target / "scripts" / "test_sam_canary.py").is_file())
            self.assertTrue(os.access(target / "verify" / "run-sam-canary.sh", os.X_OK))
            manifest_text = manifest.read_text(encoding="utf-8")
            for rel in new_paths:
                self.assertIn(rel + "\t0\t", manifest_text)

            removed = self.run_cmd(["bash", str(uninstall), str(target)], target, env)
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertEqual(before, snapshot(target))

    def test_installed_boot_index_upstream_tests_run_with_policy_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "workspace"
            target.mkdir()
            (target / "scripts").mkdir()
            shutil.copy2(
                MODULE_DIR / "tests" / "upstream" / "scripts" / "test_render_boot_index.py",
                target / "scripts" / "test_render_boot_index.py",
            )
            env = self.render_env(target)

            policy = self.run_cmd(
                ["bash", str(OVERLAY_ROOT / "modules" / "policy-pack" / "install.sh"), str(target)],
                target,
                env,
            )
            self.assertEqual(policy.returncode, 0, policy.stderr)
            prompt = self.run_cmd(["bash", str(MODULE_DIR / "install.sh"), str(target)], target, env)
            self.assertEqual(prompt.returncode, 0, prompt.stderr)

            result = self.run_cmd(
                ["python3", "-m", "unittest", "discover", "-s", "scripts", "-p", "test_render_boot_index.py"],
                target,
                env,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_installed_sam_canary_upstream_tests_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "workspace"
            target.mkdir()
            env = self.render_env(target)

            prompt = self.run_cmd(["bash", str(MODULE_DIR / "install.sh"), str(target)], target, env)
            self.assertEqual(prompt.returncode, 0, prompt.stderr)

            result = self.run_cmd(
                ["python3", "-m", "unittest", "discover", "-s", "scripts", "-p", "test_sam_canary.py"],
                target,
                env,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
