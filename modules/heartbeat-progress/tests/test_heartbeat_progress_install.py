#!/usr/bin/env python3
from __future__ import annotations

import filecmp
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1]


def snapshot(root: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(root))] = path.read_bytes()
    return result


def write_research_runtime_stub(target: Path) -> None:
    scripts = target / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "research_runtime.py").write_text(
        "def champion_metric(manifest):\n"
        "    return manifest.get('metric')\n"
        "\n"
        "def load_research_context(project):\n"
        "    return {}\n",
        encoding="utf-8",
    )


class HeartbeatProgressInstallTests(unittest.TestCase):
    def run_cmd(self, argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def import_progress_pulse(self, target: Path):
        sys.path.insert(0, str(target / "scripts"))
        try:
            spec = importlib.util.spec_from_file_location(
                "installed_progress_pulse",
                target / "scripts" / "progress-pulse.py",
            )
            self.assertIsNotNone(spec)
            module = importlib.util.module_from_spec(spec)
            assert spec is not None and spec.loader is not None
            spec.loader.exec_module(module)
            return module
        finally:
            sys.path.remove(str(target / "scripts"))

    def test_install_is_idempotent_and_uninstall_leaves_no_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "workspace"
            target.mkdir()
            (target / "baseline.txt").write_text("baseline\n", encoding="utf-8")
            write_research_runtime_stub(target)
            before = snapshot(target)

            install = MODULE_DIR / "install.sh"
            uninstall = MODULE_DIR / "uninstall.sh"

            first = self.run_cmd(["bash", str(install), str(target)], target)
            self.assertEqual(first.returncode, 0, first.stderr)
            second = self.run_cmd(["bash", str(install), str(target)], target)
            self.assertEqual(second.returncode, 0, second.stderr)

            for relpath in ("scripts/check-heartbeat.py", "scripts/progress-pulse.py"):
                source = MODULE_DIR / "src" / relpath
                self.assertTrue(filecmp.cmp(source, target / relpath, shallow=False), relpath)

            removed = self.run_cmd(["bash", str(uninstall), str(target)], target)
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertEqual(before, snapshot(target))

    def test_installed_heartbeat_checker_detects_fresh_and_stale_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "workspace"
            target.mkdir()
            write_research_runtime_stub(target)

            installed = self.run_cmd(["bash", str(MODULE_DIR / "install.sh"), str(target)], target)
            self.assertEqual(installed.returncode, 0, installed.stderr)

            fresh = target / "heartbeat-fresh.json"
            stale = target / "heartbeat-stale.json"
            fresh.write_text('{"timestamp": "' + datetime.now(timezone.utc).isoformat() + '"}\n', encoding="utf-8")
            stale.write_text(
                '{"timestamp": "' + (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat() + '"}\n',
                encoding="utf-8",
            )

            ok = self.run_cmd(["python3", "scripts/check-heartbeat.py", str(fresh), "600"], target)
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            self.assertIn("ok", ok.stdout)

            old = self.run_cmd(["python3", "scripts/check-heartbeat.py", str(stale), "600"], target)
            self.assertEqual(old.returncode, 1, old.stdout + old.stderr)
            self.assertIn("stale", old.stdout)

    def test_installed_progress_pulse_replay_suppression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "workspace"
            target.mkdir()
            write_research_runtime_stub(target)

            installed = self.run_cmd(["bash", str(MODULE_DIR / "install.sh"), str(target)], target)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            progress_pulse = self.import_progress_pulse(target)

            ctx = {
                "kind": "active",
                "issueLabel": "wave-3-primary",
                "doneCount": 0,
                "totalCount": 2,
                "currentIndex": 1,
                "signature": {"kind": "active", "epicId": "wave-3", "issueId": "primary", "doneCount": 0, "totalCount": 2},
            }

            self.assertIsNotNone(progress_pulse.build_message(ctx, {}))
            replay_state = {
                "lastSentAt": progress_pulse.now_iso(),
                "lastKind": "active",
                "lastIssueId": "wave-3-primary",
                "lastDoneCount": 0,
                "lastSignature": ctx["signature"],
            }
            self.assertIsNone(progress_pulse.build_message(ctx, replay_state))


if __name__ == "__main__":
    unittest.main()
