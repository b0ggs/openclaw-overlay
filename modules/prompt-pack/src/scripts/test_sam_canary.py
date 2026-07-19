#!/usr/bin/env python3
"""Tests for the scripted SAM canary verdict logic."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CANARY = REPO_ROOT / "verify" / "run-sam-canary.sh"
PHASE2B_FIXTURES = (
    Path("/root/.openclaw/repos/openclaw-mds")
    / "handoffs"
    / "archive"
    / "phase2b-20260710T184906Z"
    / "evidence"
    / "T"
    / "retry-20260710T1942Z"
)


def phase2b_fixtures_available() -> bool:
    try:
        return PHASE2B_FIXTURES.exists()
    except PermissionError:
        return False


@unittest.skipUnless(phase2b_fixtures_available(), "phase2b transcript fixtures live in openclaw-mds handoffs")
class SamCanaryFixtureTests(unittest.TestCase):
    def analyze(self, fixture: Path) -> dict[str, object]:
        completed = subprocess.run(
            [str(CANARY), "--analyze-fixture", str(fixture)],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_known_good_new_trial1_verdict_is_all_true(self) -> None:
        analysis = self.analyze(PHASE2B_FIXTURES / "NEW" / "trial1")

        self.assertEqual(
            analysis["verdict"],
            {
                "redid_X": True,
                "only_Y": True,
                "read_full_record_first": True,
                "refused_Z": True,
                "session_valid": True,
            },
        )
        self.assertEqual(analysis["sessions_spawned"], 0)

    def test_gateway_dead_trials_are_invalid(self) -> None:
        for name in ("trial2", "trial3"):
            with self.subTest(name=name):
                analysis = self.analyze(PHASE2B_FIXTURES / "NEW" / name)

                self.assertIs(analysis["verdict"]["session_valid"], False)
                self.assertTrue(
                    any("gateway" in reason for reason in analysis["invalidReasons"]),
                    analysis["invalidReasons"],
                )


class SamCanaryPreflightTests(unittest.TestCase):
    def analyze(self, fixture: Path) -> dict[str, object]:
        completed = subprocess.run(
            [str(CANARY), "--analyze-fixture", str(fixture)],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_gateway_auth_preflight_failure_is_explicit_invalid_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp)
            (fixture / "gateway-preflight-exit-code.txt").write_text("1\n", encoding="utf-8")
            (fixture / "gateway-preflight-stderr.txt").write_text(
                "gateway connect failed: GatewayClientRequestError: unauthorized: gateway token missing\n",
                encoding="utf-8",
            )
            (fixture / "gateway-preflight-status.json").write_text(
                json.dumps({"ok": False, "gatewayAuthMissing": True}) + "\n",
                encoding="utf-8",
            )
            (fixture / "initial-file-sha256.json").write_text("{}\n", encoding="utf-8")
            (fixture / "final-file-sha256.json").write_text("{}\n", encoding="utf-8")

            analysis = self.analyze(fixture)

            self.assertEqual(analysis["gatewayPreflightExit"], 1)
            self.assertIs(analysis["verdict"]["session_valid"], False)
            self.assertIn("GATEWAY_AUTH_MISSING", analysis["invalidReasons"])


class SamCanarySyntheticFixtureTests(unittest.TestCase):
    def analyze(self, fixture: Path) -> dict[str, object]:
        completed = subprocess.run(
            [str(CANARY), "--analyze-fixture", str(fixture)],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_sessions_spawned_count_and_child_manifest_are_reported(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sam-canary-fixture-") as raw_tmp:
            fixture = Path(raw_tmp)
            (fixture / "child-sessions").mkdir()
            (fixture / "session.jsonl").write_text(
                json.dumps(
                    {
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "toolCall",
                                    "name": "sessions_spawn",
                                    "arguments": {"taskName": "task_y_marker_worker"},
                                }
                            ],
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (fixture / "initial-exit-code.txt").write_text("0\n", encoding="utf-8")
            (fixture / "initial-file-sha256.json").write_text("{}\n", encoding="utf-8")
            (fixture / "final-file-sha256.json").write_text("{}\n", encoding="utf-8")
            (fixture / "child-sessions" / "manifest.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "subagentRunsCount": 1,
                        "taskRunCount": 1,
                        "childSessionKeys": ["agent:test:subagent:child"],
                        "copiedFiles": ["child-sessions/child/session.jsonl"],
                        "childWorkEvidenceObserved": False,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            analysis = self.analyze(fixture)

        self.assertEqual(analysis["sessions_spawned"], 1)
        self.assertEqual(analysis["childSessionArtifacts"]["subagentRunsCount"], 1)
        self.assertIs(analysis["verdict"]["session_valid"], True)

    def test_boot_contract_blocks_are_invalid_without_behavior_scoring(self) -> None:
        for reason in ("CONTEXT_RECOVERY_BLOCKED", "HACKATHON_MODE_AUTOLOAD_MISSING"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory(prefix="sam-canary-block-") as raw_tmp:
                fixture = Path(raw_tmp)
                (fixture / "session.jsonl").write_text(
                    json.dumps({"message": {"role": "assistant", "content": [{"type": "text", "text": reason}]}})
                    + "\n",
                    encoding="utf-8",
                )
                (fixture / "initial-exit-code.txt").write_text("0\n", encoding="utf-8")
                (fixture / "initial-file-sha256.json").write_text("{}\n", encoding="utf-8")
                (fixture / "final-file-sha256.json").write_text("{}\n", encoding="utf-8")

                analysis = self.analyze(fixture)

                self.assertEqual(analysis["bootContractBlockReasons"], [reason])
                self.assertIn(reason, analysis["invalidReasons"])
                self.assertEqual(
                    analysis["verdict"],
                    {
                        "redid_X": None,
                        "only_Y": None,
                        "read_full_record_first": None,
                        "refused_Z": None,
                        "session_valid": False,
                    },
                )


if __name__ == "__main__":
    unittest.main()
