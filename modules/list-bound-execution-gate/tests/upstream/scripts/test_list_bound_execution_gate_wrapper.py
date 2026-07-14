#!/usr/bin/env python3
"""Tests for the list-bound execution gate shell adapter."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "scripts" / "list-bound-execution-gate-check.sh"
PATH_HELPER = REPO_ROOT / "scripts" / "openclaw-paths.sh"


class ListBoundExecutionGateWrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.workspace = self.root / "home" / ".openclaw" / "workspace"
        self.scripts_dir = self.workspace / "scripts"
        self.scripts_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(WRAPPER, self.scripts_dir / "list-bound-execution-gate-check.sh")
        shutil.copy2(PATH_HELPER, self.scripts_dir / "openclaw-paths.sh")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_sentinel_checker(self, cli_path: Path, marker: str) -> None:
        cli_path.parent.mkdir(parents=True, exist_ok=True)
        cli_path.write_text(
            "#!/usr/bin/env node\n"
            "const payload = { marker: process.env.SENTINEL_MARKER, argv: process.argv.slice(2) };\n"
            "console.log(JSON.stringify(payload));\n",
            encoding="utf-8",
        )
        cli_path.chmod(0o755)

    def run_wrapper(self, *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.scripts_dir / "list-bound-execution-gate-check.sh"), "issue-a", "dispatch"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def base_env(self, marker: str) -> dict[str, str]:
        env = {
            **os.environ,
            "OPENCLAW_WORKSPACE_ROOT": str(self.workspace),
            "SENTINEL_MARKER": marker,
        }
        env.pop("OPENCLAW_OVERLAY_V2_ROOT", None)
        return env

    def test_unset_overlay_root_resolves_vendored_checker_relative_to_wrapper(self) -> None:
        self.write_sentinel_checker(
            self.scripts_dir / "list-bound-execution-gate" / "src" / "cli.js",
            "vendored",
        )

        result = self.run_wrapper(env=self.base_env("vendored"))

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["marker"], "vendored")
        self.assertEqual(
            payload["argv"],
            ["check", "--workspace", str(self.workspace), "--issue-id", "issue-a", "--mode", "dispatch"],
        )

    def test_explicit_overlay_root_still_overrides_vendored_checker(self) -> None:
        self.write_sentinel_checker(
            self.scripts_dir / "list-bound-execution-gate" / "src" / "cli.js",
            "vendored",
        )
        overlay_root = self.root / "overlay-v2"
        self.write_sentinel_checker(
            overlay_root / "modules" / "list-bound-execution-gate" / "src" / "cli.js",
            "override",
        )
        env = self.base_env("override")
        env["OPENCLAW_OVERLAY_V2_ROOT"] = str(overlay_root)

        result = self.run_wrapper(env=env)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["marker"], "override")


if __name__ == "__main__":
    unittest.main()
