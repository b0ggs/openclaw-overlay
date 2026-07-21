#!/usr/bin/env python3
"""Tests for the scripted SAM canary verdict logic."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CANARY = REPO_ROOT / "verify" / "run-sam-canary.sh"
RERUN = REPO_ROOT / "verify" / "run-sam-canary-rerun.sh"
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


class SamCanaryRunnerProvenanceTests(unittest.TestCase):
    def analyze(self, fixture: Path, env: dict[str, str] | None = None) -> dict[str, object]:
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        completed = subprocess.run(
            [str(CANARY), "--analyze-fixture", str(fixture)],
            cwd=REPO_ROOT,
            env=full_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_analysis_reports_local_runner_provenance(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sam-canary-provenance-") as raw_tmp:
            fixture = Path(raw_tmp)
            write_clean_fixture(fixture)

            analysis = self.analyze(fixture)

        provenance = analysis["runnerProvenance"]
        self.assertEqual(provenance["runnerPath"], str(CANARY.resolve()))
        self.assertEqual(provenance["runnerSha256"], hashlib.sha256(CANARY.read_bytes()).hexdigest())
        self.assertTrue(provenance["runnerExecutable"])
        self.assertEqual(
            provenance["markers"],
            {"trusted_infra_text": True, "infra_invalid_reasons": True},
        )

    def test_runner_provenance_redacts_inline_url_credentials(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sam-canary-provenance-redact-") as raw_tmp:
            fixture = Path(raw_tmp)
            write_clean_fixture(fixture)

            analysis = self.analyze(
                fixture,
                {
                    "SAM_CANARY_OVERLAY_REPO": "https://user:password@example.invalid/repo.git",
                    "SAM_CANARY_EXECUTION_WORKSPACE": "https://worker:secret@example.invalid/workspace",
                },
            )

        provenance = analysis["runnerProvenance"]
        serialized = json.dumps(provenance, sort_keys=True)
        self.assertNotIn("password", serialized)
        self.assertNotIn("secret", serialized)
        self.assertEqual(provenance["overlayRepo"], "https://<redacted>@example.invalid/repo.git")
        self.assertEqual(provenance["executionWorkspace"], "https://<redacted>@example.invalid/workspace")


class SamCanaryRerunDriverTests(unittest.TestCase):
    def run_driver(
        self,
        argv: list[str],
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        completed = subprocess.run(
            [str(RERUN), *argv],
            cwd=cwd,
            env=full_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        payload = json.loads(completed.stdout)
        return completed, payload

    def make_overlay_repo(
        self,
        root: Path,
        *,
        runner_text: str,
        expected_sha: str | None = None,
    ) -> tuple[Path, str]:
        repo = root / "overlay"
        src_verify = repo / "modules" / "prompt-pack" / "src" / "verify"
        src_verify.mkdir(parents=True)
        runner = src_verify / "run-sam-canary.sh"
        runner.write_text(runner_text, encoding="utf-8")
        runner.chmod(0o755)
        expected = expected_sha or hashlib.sha256(runner_text.encode("utf-8")).hexdigest()
        (repo / "modules" / "prompt-pack" / "module.yaml").write_text(
            "\n".join(
                [
                    "module: prompt-pack",
                    "files:",
                    "  - source_path: verify/run-sam-canary.sh",
                    "    overlay_path: modules/prompt-pack/src/verify/run-sam-canary.sh",
                    "    install_path: verify/run-sam-canary.sh",
                    "    kind: executable-verification-canary",
                    '    mode: "0755"',
                    f"    sha256: {expected}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        install = repo / "modules" / "prompt-pack" / "install.sh"
        install.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    'TARGET="${1:-$PWD}"',
                    'MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"',
                    'mkdir -p "$TARGET/verify"',
                    'cp "$MODULE_DIR/src/verify/run-sam-canary.sh" "$TARGET/verify/run-sam-canary.sh"',
                    'chmod 0755 "$TARGET/verify/run-sam-canary.sh"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        install.chmod(0o755)
        subprocess.run(["git", "init"], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        subprocess.run(["git", "add", "."], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=SAM Canary Test",
                "-c",
                "user.email=sam-canary@example.invalid",
                "commit",
                "-m",
                "synthetic overlay",
            ],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout.strip()
        return repo, commit

    def test_rerun_driver_rejects_short_overlay_commit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sam-rerun-short-") as raw_tmp:
            root = Path(raw_tmp)
            repo, _commit = self.make_overlay_repo(root, runner_text=CANARY.read_text(encoding="utf-8"))

            completed, payload = self.run_driver(
                [
                    "--overlay-repo",
                    str(repo),
                    "--overlay-commit",
                    "main",
                    "--out-dir",
                    str(root / "out"),
                    "--preflight-only",
                ],
                root,
            )

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertIn("full 40-character", payload["blockedReason"])
        self.assertFalse(payload["gatewayOrAgentLaunchAttempted"])

    def test_rerun_driver_blocks_stale_runner_sha_before_launch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sam-rerun-stale-") as raw_tmp:
            root = Path(raw_tmp)
            expected_sha = hashlib.sha256(CANARY.read_bytes()).hexdigest()
            repo, commit = self.make_overlay_repo(
                root,
                runner_text="#!/usr/bin/env bash\nset -euo pipefail\necho stale runner\n",
                expected_sha=expected_sha,
            )

            completed, payload = self.run_driver(
                [
                    "--overlay-repo",
                    str(repo),
                    "--overlay-commit",
                    commit,
                    "--work-root",
                    str(root / "work"),
                    "--preflight-only",
                ],
                root,
            )

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertIn("runner SHA-256 mismatch", payload["blockedReason"])
        self.assertFalse(payload["gatewayOrAgentLaunchAttempted"])
        self.assertFalse(payload["runnerProvenance"]["expectedRunnerSha256Matches"])

    def test_rerun_driver_blocks_marker_missing_runner_before_launch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sam-rerun-marker-") as raw_tmp:
            root = Path(raw_tmp)
            repo, commit = self.make_overlay_repo(
                root,
                runner_text="#!/usr/bin/env bash\nset -euo pipefail\necho no marker surface\n",
            )

            completed, payload = self.run_driver(
                [
                    "--overlay-repo",
                    str(repo),
                    "--overlay-commit",
                    commit,
                    "--work-root",
                    str(root / "work"),
                    "--preflight-only",
                ],
                root,
            )

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertIn("missing required provenance markers", payload["blockedReason"])
        self.assertFalse(payload["gatewayOrAgentLaunchAttempted"])
        self.assertEqual(
            payload["runnerProvenance"]["markers"],
            {"trusted_infra_text": False, "infra_invalid_reasons": False},
        )

    def test_rerun_driver_rejects_external_execution_workspace_before_install(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sam-rerun-exec-workspace-") as raw_tmp:
            root = Path(raw_tmp)
            repo, commit = self.make_overlay_repo(root, runner_text=CANARY.read_text(encoding="utf-8"))
            external_workspace = root / "external-execution-workspace"
            external_workspace.mkdir()
            sentinel = external_workspace / "sentinel.txt"
            sentinel.write_text("do not overwrite\n", encoding="utf-8")

            completed, payload = self.run_driver(
                [
                    "--overlay-repo",
                    str(repo),
                    "--overlay-commit",
                    commit,
                    "--work-root",
                    str(root / "work"),
                    "--execution-workspace",
                    str(external_workspace),
                    "--preflight-only",
                ],
                root,
            )
            sentinel_text = sentinel.read_text(encoding="utf-8")

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertIn("execution workspace must be under work root", payload["blockedReason"])
        self.assertFalse(payload["gatewayOrAgentLaunchAttempted"])
        self.assertEqual(sentinel_text, "do not overwrite\n")

    def test_rerun_driver_rejects_archive_symlink_member(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sam-rerun-archive-symlink-") as raw_tmp:
            root = Path(raw_tmp)
            archive = root / "overlay.tar"
            with tarfile.open(archive, "w") as tf:
                directory = tarfile.TarInfo("overlay/")
                directory.type = tarfile.DIRTYPE
                tf.addfile(directory)
                symlink = tarfile.TarInfo("overlay/link")
                symlink.type = tarfile.SYMTYPE
                symlink.linkname = "/tmp/outside-openclaw-sam-canary"
                tf.addfile(symlink)
            archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()

            completed, payload = self.run_driver(
                [
                    "--archive",
                    str(archive),
                    "--archive-sha256",
                    archive_sha,
                    "--work-root",
                    str(root / "work"),
                    "--preflight-only",
                ],
                root,
            )

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertIn("archive member type not allowed", payload["blockedReason"])
        self.assertFalse(payload["gatewayOrAgentLaunchAttempted"])

    def test_rerun_driver_preflight_only_does_not_invoke_openclaw_or_nohup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sam-rerun-no-launch-") as raw_tmp:
            root = Path(raw_tmp)
            repo, commit = self.make_overlay_repo(root, runner_text=CANARY.read_text(encoding="utf-8"))
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            invocation_log = root / "unexpected-launch.log"
            for name in ("openclaw", "nohup"):
                path = fake_bin / name
                path.write_text(
                    f"#!/usr/bin/env bash\nprintf '%s\\n' {name} >> {invocation_log!s}\nexit 77\n",
                    encoding="utf-8",
                )
                path.chmod(0o755)

            completed, payload = self.run_driver(
                [
                    "--overlay-repo",
                    str(repo),
                    "--overlay-commit",
                    commit,
                    "--work-root",
                    str(root / "work"),
                    "--preflight-only",
                ],
                root,
                {"PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]},
            )
            unexpected_invoked = invocation_log.exists()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(payload["status"], "PREFLIGHT_OK")
        self.assertEqual(payload["launchStatus"], "not_launched_preflight_only")
        self.assertFalse(payload["launchAttempted"])
        self.assertFalse(payload["gatewayOrAgentLaunchAttempted"])
        self.assertFalse(unexpected_invoked)
        self.assertTrue(payload["canaryCommandVerified"])
        self.assertIn("/verify/run-sam-canary.sh", payload["canaryCommand"][0])


if __name__ == "__main__":
    unittest.main()
