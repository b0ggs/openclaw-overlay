from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


preflight = load_module("openclaw_runtime_preflight", REPO_ROOT / "scripts" / "openclaw-runtime-preflight.py")


GOOD_MODELS_STATUS = {
    "defaultModel": "openai-codex/gpt-5.5",
    "resolvedDefault": "openai-codex/gpt-5.5",
    "allowed": ["openai-codex/gpt-5.5"],
    "fallbacks": [],
}

GOOD_CODEX_AGENT_MODELS_STATUS = {
    **GOOD_MODELS_STATUS,
    "agentId": "codex",
}

GOOD_RUNTIME_STATUS = {
    "sessions": [
        {
            "agent": "codex",
            "provider": "openai-codex",
            "model": "gpt-5.5",
            "agentRuntime": {"id": "pi", "name": "OpenClaw Pi Default"},
        }
    ]
}

SHORT_MODEL_PI_RUNTIME_STATUS = {
    "sessions": [
        {
            "agent": "codex",
            "model": "gpt-5.5",
            "agentRuntime": {"id": "pi", "name": "OpenClaw Pi Default"},
        }
    ]
}


class OpenClawRuntimePreflightTests(unittest.TestCase):
    def assert_classification(self, argv: tuple[str, ...], expected: str) -> None:
        result = preflight.classify_command(argv)
        self.assertEqual(result.classification, expected, argv)

    def assert_non_runnable(self, argv: tuple[str, ...], expected: str | None = None) -> None:
        result = preflight.classify_command(argv)
        if expected is not None:
            self.assertEqual(result.classification, expected, argv)
        self.assertFalse(result.runnable, argv)

    def test_read_only_command_classification(self) -> None:
        read_only_commands = [
            ("git", "status", "--short", "--branch"),
            ("git", "diff", "--check"),
            ("scripts/git-preflight.sh", "."),
            ("openclaw", "models", "status", "--json"),
            ("openclaw", "models", "status", "--agent", "codex", "--json"),
            ("openclaw", "sessions", "--all-agents", "--active", "60", "--limit", "10", "--json"),
            ("openclaw", "memory", "status", "--deep", "--json"),
        ]
        for argv in read_only_commands:
            with self.subTest(argv=argv):
                self.assert_classification(argv, preflight.READ_ONLY)

    def test_py_compile_is_classified_as_repo_mutation(self) -> None:
        result = preflight.classify_command(("python3", "-m", "py_compile", "scripts/openclaw-runtime-preflight.py"))
        self.assertEqual(result.classification, preflight.REPO_MUTATION)
        self.assertFalse(result.runnable)

    def test_forbidden_command_classification(self) -> None:
        forbidden = [
            ("openclaw", "doctor", "--fix"),
            ("openclaw", "doctor", "--repair"),
            ("openclaw", "doctor", "--deep"),
            ("openclaw", "update", "--repair"),
            ("openclaw", "models", "migrate", "--to", "native-codex"),
            ("openclaw", "codex", "migrate", "--runtime", "native"),
            ("openclaw", "plugins", "registry", "--json"),
            ("openclaw", "gateway", "restart"),
            ("openclaw", "gateway", "stop"),
            ("openclaw", "channels", "enable", "webchat"),
            ("openclaw", "webhooks", "set", "https://example.invalid/hook"),
            ("openclaw", "dashboard", "expose", "--public"),
            ("openclaw", "browser", "expose", "--public"),
            ("openclaw", "sandbox", "recreate", "--all", "--force"),
            ("openclaw", "memory", "status", "--deep", "--index", "--json"),
            ("openclaw", "nodes", "pair", "--setup-code", "123"),
        ]
        for argv in forbidden:
            with self.subTest(argv=argv):
                self.assert_classification(argv, preflight.FORBIDDEN_NOT_RUN)

    def test_config_and_service_mutations_are_non_runnable(self) -> None:
        cases = [
            (("openclaw", "config", "patch", "models.default=openai/gpt-4.1"), preflight.LIVE_CONFIG_MUTATION),
            (("openclaw", "config", "apply", "candidate.json"), preflight.LIVE_CONFIG_MUTATION),
            (("openclaw", "config", "set", "gateway.host=0.0.0.0"), preflight.LIVE_CONFIG_MUTATION),
            (("openclaw", "config", "edit"), preflight.LIVE_CONFIG_MUTATION),
            (("systemctl", "restart", "openclaw-gateway"), preflight.SERVICE_OR_EXPOSURE_MUTATION),
            (("service", "openclaw-gateway", "restart"), preflight.SERVICE_OR_EXPOSURE_MUTATION),
            (("caddy", "reload"), preflight.SERVICE_OR_EXPOSURE_MUTATION),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.assert_non_runnable(argv, expected)

    def test_shell_control_chaining_and_redirection_are_non_runnable(self) -> None:
        cases = [
            ("openclaw", "models", "status", "--json", "&&", "openclaw", "config", "patch", "x=y"),
            ("git", "status", "--short", "--branch", ";", "systemctl", "restart", "openclaw-gateway"),
            ("openclaw", "models", "status", "--json", "|", "jq", "."),
            ("openclaw", "models", "status", "--json", "||", "openclaw", "status"),
            ("openclaw", "models", "status", "--json", ">", "/tmp/openclaw-models.json"),
            ("openclaw", "models", "status", "--json", ">>", "/tmp/openclaw-models.json"),
            ("openclaw", "models", "status", "--json", "2>/tmp/openclaw-models.err"),
            ("openclaw", "models", "status", "--json", "<", "/tmp/input.json"),
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                self.assert_non_runnable(argv, preflight.FORBIDDEN_NOT_RUN)

    def test_shell_wrappers_are_non_runnable(self) -> None:
        cases = [
            ("bash", "-lc", "openclaw models status --json"),
            ("sh", "-c", "git status --short --branch; systemctl restart openclaw-gateway"),
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                self.assert_non_runnable(argv, preflight.FORBIDDEN_NOT_RUN)

    def test_forbidden_examples_are_never_runnable(self) -> None:
        for argv in preflight.forbidden_command_examples():
            with self.subTest(argv=argv):
                self.assert_non_runnable(argv)

    def test_workspace_openclaw_json_is_non_authoritative(self) -> None:
        workspace_config = str(preflight.NON_AUTHORITATIVE_WORKSPACE_CONFIG)
        result = preflight.classify_command(("cat", workspace_config))
        self.assertEqual(result.classification, preflight.FORBIDDEN_NOT_RUN)
        self.assertFalse(result.runnable)
        self.assertIn("non-authoritative", result.reason)
        with self.assertRaisesRegex(ValueError, "non-authoritative"):
            preflight._read_json(workspace_config)
        self.assert_classification(("openclaw", "config", "file"), preflight.READ_ONLY)
        self.assert_classification(("openclaw", "config", "validate", "--json"), preflight.READ_ONLY)

    def test_baseline_preflight_contains_only_read_only_commands(self) -> None:
        for spec in preflight.build_preflight_commands(agent="codex", include_session_route=True):
            with self.subTest(argv=spec.argv):
                classification = preflight.classify_command(spec.argv)
                self.assertEqual(classification.classification, preflight.READ_ONLY)
                self.assertTrue(classification.runnable)

    def test_route_guard_accepts_expected_model_and_pi_runtime(self) -> None:
        result = preflight.check_route_guard(GOOD_MODELS_STATUS, runtime_status=GOOD_RUNTIME_STATUS)
        self.assertTrue(result.accepted, result.reasons)
        self.assertTrue(result.summary["hasPiRuntimeEvidence"])
        self.assertTrue(result.summary["hasExpectedModelPiRuntimeEvidence"])

    def test_route_guard_accepts_short_model_codex_pi_runtime_with_agent_model_proof(self) -> None:
        result = preflight.check_route_guard(
            GOOD_MODELS_STATUS,
            runtime_status=SHORT_MODEL_PI_RUNTIME_STATUS,
            agent_model_status=GOOD_CODEX_AGENT_MODELS_STATUS,
        )
        self.assertTrue(result.accepted, result.reasons)
        self.assertTrue(result.summary["agentModelStatusProvesExpectedModel"])
        self.assertTrue(result.summary["hasExpectedModelPiRuntimeEvidence"])

    def test_route_guard_rejects_short_model_pi_runtime_without_agent_model_proof(self) -> None:
        cases = [None, GOOD_MODELS_STATUS]
        for agent_model_status in cases:
            with self.subTest(agent_model_status=agent_model_status):
                result = preflight.check_route_guard(
                    GOOD_MODELS_STATUS,
                    runtime_status=SHORT_MODEL_PI_RUNTIME_STATUS,
                    agent_model_status=agent_model_status,
                )
                self.assertFalse(result.accepted)
                self.assertFalse(result.summary["agentModelStatusProvesExpectedModel"])
                self.assertIn("missing_expected_model_pi_runtime_evidence", result.reasons)

    def test_route_guard_rejects_short_model_pi_runtime_for_unrelated_agent_model_proof(self) -> None:
        unrelated_agent_status = dict(GOOD_MODELS_STATUS)
        unrelated_agent_status["agent"] = "auditor-alpha"
        result = preflight.check_route_guard(
            GOOD_MODELS_STATUS,
            runtime_status=SHORT_MODEL_PI_RUNTIME_STATUS,
            agent_model_status=unrelated_agent_status,
        )
        self.assertFalse(result.accepted)
        self.assertFalse(result.summary["agentModelStatusProvesExpectedModel"])
        self.assertIn("missing_expected_model_pi_runtime_evidence", result.reasons)

    def test_route_guard_rejects_short_model_pi_runtime_for_wrong_runtime_agent(self) -> None:
        runtime_status = {
            "sessions": [
                {
                    "agent": "auditor-alpha",
                    "model": "gpt-5.5",
                    "agentRuntime": {"id": "pi", "name": "OpenClaw Pi Default"},
                }
            ]
        }
        result = preflight.check_route_guard(
            GOOD_MODELS_STATUS,
            runtime_status=runtime_status,
            agent_model_status=GOOD_CODEX_AGENT_MODELS_STATUS,
        )
        self.assertFalse(result.accepted)
        self.assertTrue(result.summary["agentModelStatusProvesExpectedModel"])
        self.assertFalse(result.summary["hasExpectedModelPiRuntimeEvidence"])
        self.assertFalse(result.summary["hasExpectedAgentRuntimeEvidence"])
        self.assertIn("missing_expected_model_pi_runtime_evidence", result.reasons)

    def test_route_guard_blocks_present_allowed_list_drift(self) -> None:
        cases = [
            ("allowed", []),
            ("allowedModels", []),
            ("allowedModels", ["openai/gpt-4.1"]),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                payload = {field: item for field, item in GOOD_MODELS_STATUS.items() if field != "allowed"}
                payload[key] = value
                result = preflight.check_route_guard(payload, runtime_status=GOOD_RUNTIME_STATUS)
                self.assertFalse(result.accepted)
                self.assertIn("default_allowed_models_mismatch", result.reasons)

    def test_route_guard_blocks_default_model_drift(self) -> None:
        payload = dict(GOOD_MODELS_STATUS)
        payload["resolvedDefault"] = "openai/gpt-4.1"
        result = preflight.check_route_guard(payload, runtime_status=GOOD_RUNTIME_STATUS)
        self.assertFalse(result.accepted)
        self.assertTrue(any("resolvedDefault_mismatch" in reason for reason in result.reasons))

    def test_route_guard_blocks_agent_model_drift(self) -> None:
        agent_payload = dict(GOOD_MODELS_STATUS)
        agent_payload["defaultModel"] = "openai-codex/gpt-5.5"
        agent_payload["resolvedDefault"] = "openai/gpt-4.1"
        result = preflight.check_route_guard(
            GOOD_MODELS_STATUS,
            runtime_status=GOOD_RUNTIME_STATUS,
            agent_model_status=agent_payload,
        )
        self.assertFalse(result.accepted)
        self.assertTrue(any(reason.startswith("agent_resolvedDefault_mismatch") for reason in result.reasons))

    def test_route_guard_blocks_missing_pi_runtime_evidence(self) -> None:
        result = preflight.check_route_guard(GOOD_MODELS_STATUS, runtime_status={"sessions": []})
        self.assertFalse(result.accepted)
        self.assertIn("missing_pi_runtime_evidence", result.reasons)

    def test_route_guard_requires_codex_agent_pi_runtime_evidence(self) -> None:
        runtime_status = {
            "sessions": [
                {
                    "agent": "auditor-alpha",
                    "provider": "openai-codex",
                    "model": "gpt-5.5",
                    "agentRuntime": {"id": "pi", "name": "OpenClaw Pi Default"},
                }
            ]
        }
        result = preflight.check_route_guard(GOOD_MODELS_STATUS, runtime_status=runtime_status)
        self.assertFalse(result.accepted)
        self.assertTrue(result.summary["hasPiRuntimeEvidence"])
        self.assertFalse(result.summary["hasExpectedModelPiRuntimeEvidence"])
        self.assertFalse(result.summary["hasExpectedAgentRuntimeEvidence"])
        self.assertIn("missing_expected_model_pi_runtime_evidence", result.reasons)

    def test_route_guard_blocks_split_expected_model_and_unrelated_pi_evidence(self) -> None:
        runtime_status = {
            "sessions": [
                {
                    "provider": "openai-codex",
                    "model": "gpt-5.5",
                    "agentRuntime": {"id": "native", "name": "Native Codex"},
                },
                {
                    "provider": "openai",
                    "model": "gpt-4.1",
                    "agentRuntime": {"id": "pi", "name": "OpenClaw Pi Default"},
                },
            ]
        }
        result = preflight.check_route_guard(GOOD_MODELS_STATUS, runtime_status=runtime_status)
        self.assertFalse(result.accepted)
        self.assertTrue(result.summary["hasPiRuntimeEvidence"])
        self.assertFalse(result.summary["hasExpectedModelPiRuntimeEvidence"])
        self.assertIn("missing_expected_model_pi_runtime_evidence", result.reasons)

    def test_forbidden_command_is_not_run_by_runner(self) -> None:
        calls: list[list[str]] = []

        def fake_runner(argv, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        spec = preflight.CommandSpec(("openclaw", "doctor", "--deep"), "must not run")
        run = preflight._run_command(spec, cwd=REPO_ROOT, runner=fake_runner)
        self.assertTrue(run.skipped)
        self.assertEqual(run.classification, preflight.FORBIDDEN_NOT_RUN)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
