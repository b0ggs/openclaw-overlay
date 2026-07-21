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
INFRA_PATTERN_EXCERPTS = (
    "gateway closed",
    "gateway token mismatch",
    "gateway connect failed",
    "unauthorized: gateway",
    "subagent run lost active execution context",
    '"status": "error"',
    "sessions_spawn failed",
    "hard stop while dispatching",
    "timeout while running command",
)


def write_clean_fixture(
    fixture: Path,
    *,
    session_text: str = "clean canary transcript",
    initial_exit: int = 0,
    initial_stderr: str = "",
    initial_stdout: object | None = None,
    followup_exit: int | None = None,
    followup_stderr: str = "",
    gateway_stdout: str = "",
    gateway_stderr: str = "",
    gateway_status: dict[str, object] | None = None,
) -> None:
    status = gateway_status if gateway_status is not None else {"ok": True, "gatewayAuthMissing": False}
    assistant_row = {"message": {"role": "assistant", "content": [{"type": "text", "text": session_text}]}}
    tool_row = {"message": {"role": "tool", "content": [{"type": "toolResult", "text": session_text}]}}
    (fixture / "gateway-preflight-exit-code.txt").write_text("0\n", encoding="utf-8")
    (fixture / "gateway-preflight-stdout.txt").write_text(gateway_stdout, encoding="utf-8")
    (fixture / "gateway-preflight-stderr.txt").write_text(gateway_stderr, encoding="utf-8")
    (fixture / "gateway-preflight-status.json").write_text(json.dumps(status) + "\n", encoding="utf-8")
    (fixture / "session.jsonl").write_text(json.dumps(assistant_row) + "\n", encoding="utf-8")
    (fixture / "full-transcript.jsonl").write_text(json.dumps(tool_row) + "\n", encoding="utf-8")
    (fixture / "initial-exit-code.txt").write_text(f"{initial_exit}\n", encoding="utf-8")
    (fixture / "initial-stdout.json").write_text(json.dumps(initial_stdout or {}) + "\n", encoding="utf-8")
    (fixture / "initial-stderr.txt").write_text(initial_stderr, encoding="utf-8")
    if followup_exit is not None:
        (fixture / "followup-exit-code.txt").write_text(f"{followup_exit}\n", encoding="utf-8")
        (fixture / "followup-stderr.txt").write_text(followup_stderr, encoding="utf-8")
    (fixture / "initial-file-sha256.json").write_text("{}\n", encoding="utf-8")
    (fixture / "final-file-sha256.json").write_text("{}\n", encoding="utf-8")


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

    def test_phase2b_transcript_only_gateway_strings_do_not_create_gateway_invalid_reasons(self) -> None:
        for name in ("trial2", "trial3"):
            with self.subTest(name=name):
                analysis = self.analyze(PHASE2B_FIXTURES / "NEW" / name)

                self.assertIs(analysis["verdict"]["session_valid"], False)
                self.assertEqual(analysis["invalidReasons"], ["missing child session evidence"])
                self.assertFalse(any("gateway" in reason for reason in analysis["invalidReasons"]))


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

    def test_trusted_preflight_status_and_text_are_infra_invalid_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp)
            write_clean_fixture(
                fixture,
                gateway_stdout="gateway token mismatch while checking gateway status\n",
                gateway_stderr="GATEWAY_AUTH_MISSING\n",
                gateway_status={"ok": False, "details": {"gatewayAuthMissing": True}},
            )

            analysis = self.analyze(fixture)

            self.assertIs(analysis["verdict"]["session_valid"], False)
            self.assertIn("GATEWAY_AUTH_MISSING", analysis["invalidReasons"])
            self.assertIn("gateway token mismatch", analysis["invalidReasons"])

    def test_gateway_auth_missing_sentinel_is_infra_invalid_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp)
            write_clean_fixture(fixture)
            (fixture / "GATEWAY_AUTH_MISSING.txt").write_text("gateway auth missing\n", encoding="utf-8")

            analysis = self.analyze(fixture)

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

    def test_transcript_and_clean_exit_outputs_do_not_create_infra_invalid_reasons(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sam-canary-false-positive-") as raw_tmp:
            fixture = Path(raw_tmp)
            source_like_text = "\n".join(
                [
                    "quoted analyzer source mentions GATEWAY_AUTH_MISSING",
                    "INFRA_ERROR_PATTERNS = (",
                    *INFRA_PATTERN_EXCERPTS,
                    ")",
                ]
            )
            write_clean_fixture(
                fixture,
                session_text=source_like_text,
                initial_stdout={"message": source_like_text, "toolResult": source_like_text},
                initial_stderr=source_like_text,
            )

            analysis = self.analyze(fixture)

        self.assertEqual(analysis["invalidReasons"], [])
        self.assertIs(analysis["verdict"]["session_valid"], True)

    def test_top_level_command_stdout_error_is_infra_invalid_reason(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sam-canary-stdout-error-") as raw_tmp:
            fixture = Path(raw_tmp)
            write_clean_fixture(
                fixture,
                initial_stdout={"status": "error", "error": {"message": "gateway connect failed"}},
            )

            analysis = self.analyze(fixture)

        self.assertIs(analysis["verdict"]["session_valid"], False)
        self.assertIn('"status": "error"', analysis["invalidReasons"])
        self.assertIn("gateway connect failed", analysis["invalidReasons"])

    def test_nonzero_turn_stderr_infra_pattern_is_invalid_reason(self) -> None:
        cases = (
            ("initial", {"initial_exit": 1, "initial_stderr": "sessions_spawn failed\n"}, "sessions_spawn failed"),
            ("followup", {"followup_exit": 1, "followup_stderr": "gateway closed\n"}, "gateway closed"),
        )
        for label, kwargs, expected_reason in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory(prefix="sam-canary-stderr-") as raw_tmp:
                fixture = Path(raw_tmp)
                write_clean_fixture(fixture, **kwargs)

                analysis = self.analyze(fixture)

                self.assertIs(analysis["verdict"]["session_valid"], False)
                self.assertIn(expected_reason, analysis["invalidReasons"])
                self.assertIn("outer command exit was nonzero", analysis["invalidReasons"])

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
