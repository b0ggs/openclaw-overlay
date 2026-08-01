import json
import os
import pathlib
import stat
import subprocess
import tempfile
import time
import unittest


MODULE = pathlib.Path(__file__).resolve().parents[1]
MONITOR = MODULE / "scripts/lane-guard-healthmon.mjs"


FAKE = r"""#!/usr/bin/env python3
import json, os, pathlib, sys, time
args = sys.argv[1:]
log = pathlib.Path(os.environ["FAKE_LOG"])
with log.open("a", encoding="utf-8") as out:
    out.write(json.dumps(args) + "\n")
mode = os.environ.get("FAKE_MODE", "healthy")
if args[:2] == ["plugins", "inspect"]:
    gate = os.environ.get("FAKE_INSPECT_GATE")
    if gate:
        deadline = time.monotonic() + 10
        while not pathlib.Path(gate).exists() and time.monotonic() < deadline:
            time.sleep(0.02)
    if mode == "cold-fail":
        sys.exit(1)
    print(json.dumps({
        "plugin": {
            "id": "workspace-lane-guard", "version": "0.1.0",
            "contracts": {"trustedToolPolicies": ["workspace-lane-admission"]},
            "enabled": True,
        }
    }))
    sys.exit(0)
if args[:3] == ["gateway", "call", "sessions.list"]:
    if mode in {"gateway-fail", "cold-fail", "session-list-fail"}:
        sys.exit(1)
    if mode == "no-eligible":
        print(json.dumps({"sessions": [], "hasMore": False}))
        sys.exit(0)
    print(json.dumps({"sessions": [
        {
            "key": "agent:main:subagent:newest-child", "kind": "direct",
            "spawnedBy": "agent:main:older", "lastInteractionAt": 500,
        },
        {
            "key": "agent:main:older", "kind": "direct",
            "lastInteractionAt": 300,
        },
        {
            "key": "agent:main:newest", "kind": "direct",
            "lastInteractionAt": 400,
        },
        {
            "key": "agent:main:group", "kind": "group",
            "lastInteractionAt": 600,
        },
    ], "hasMore": False}))
    sys.exit(0)
if args[:3] == ["gateway", "call", "plugins.sessionAction"]:
    if mode in {"gateway-fail", "cold-fail"}:
        sys.exit(1)
    params = json.loads(args[args.index("--params") + 1])
    challenge = params["payload"]["challenge"]
    ready = mode != "component-fail"
    print(json.dumps({"result": {"result": {
        "schemaVersion": 1, "ready": ready,
        "pluginId": "workspace-lane-guard", "pluginVersion": "0.1.0",
        "policyId": "workspace-lane-admission", "policyRegistered": ready,
        "workerReady": ready, "reconcilerReady": ready,
        "gatewayGenerationId": "generation-123",
        "gatewayStartedAt": 10, "lastReconciledAt": 20,
        "challenge": challenge,
    }}}))
    sys.exit(0)
if args[:2] == ["system", "event"]:
    delay = float(os.environ.get("FAKE_EVENT_DELAY", "0"))
    if delay:
        time.sleep(delay)
    if mode in {"gateway-fail", "cold-fail", "event-fail"}:
        sys.exit(1)
    sys.exit(0)
sys.exit(2)
"""


class HealthMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wlg-health-")
        self.root = pathlib.Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fake = self.bin / "openclaw"
        fake.write_text(FAKE, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.log = self.root / "calls.jsonl"
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "OPENCLAW_STATE_DIR": str(self.root / "state"),
            "FAKE_LOG": str(self.log),
            "LANE_GUARD_HEALTH_TIMEOUT_MS": "1000",
        }

    def tearDown(self):
        self.temp.cleanup()

    def run_monitor(self, mode="healthy", **extra_env):
        env = {**self.env, "FAKE_MODE": mode, **extra_env}
        return subprocess.run(["node", str(MONITOR)], env=env, text=True, capture_output=True)

    def state(self):
        path = self.root / "state/plugins/workspace-lane-guard/health/incident.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_healthy_requires_cold_and_fresh_live_challenge(self):
        result = self.run_monitor()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("HEALTHY generation-123", result.stdout)
        self.assertFalse(self.state()["active"])
        flat = json.dumps(self.calls())
        self.assertNotIn("--runtime", flat)

    def test_dead_gateway_persists_incident_and_exits_nonzero(self):
        result = self.run_monitor("gateway-fail")
        self.assertNotEqual(result.returncode, 0)
        state = self.state()
        self.assertTrue(state["active"])
        self.assertEqual(state["reasonCode"], "LIVE_RPC_FAILED")
        incident = state["incidentId"]
        self.run_monitor("gateway-fail")
        self.assertEqual(self.state()["incidentId"], incident)

    def test_component_failure_is_never_healthy(self):
        result = self.run_monitor("component-fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state()["reasonCode"], "LIVE_READINESS_MISMATCH")

    def test_recovery_clears_warning_and_delivers_alert_once(self):
        self.run_monitor("gateway-fail")
        failed = self.state()
        self.assertTrue(failed["active"])
        self.assertFalse(failed["delivered"])
        self.assertTrue(failed["alertPending"])
        result = self.run_monitor("healthy")
        self.assertEqual(result.returncode, 0)
        recovered = self.state()
        self.assertFalse(recovered["active"])
        self.assertEqual(recovered["recoveredIncidentId"], failed["incidentId"])
        self.assertTrue(recovered["delivered"])
        self.assertFalse(recovered["alertPending"])
        events = [call for call in self.calls() if call[:2] == ["system", "event"]]
        self.assertEqual(len(events), 1)
        self.run_monitor("healthy")
        events = [call for call in self.calls() if call[:2] == ["system", "event"]]
        self.assertEqual(len(events), 1)

    def test_recovery_delivery_failure_remains_pending_until_exactly_one_success(self):
        self.run_monitor("gateway-fail")
        incident = self.state()["incidentId"]
        first_recovery = self.run_monitor("event-fail")
        self.assertNotEqual(first_recovery.returncode, 0)
        pending = self.state()
        self.assertFalse(pending["active"])
        self.assertEqual(pending["recoveredIncidentId"], incident)
        self.assertFalse(pending["delivered"])
        self.assertTrue(pending["alertPending"])
        successful_retry = self.run_monitor("healthy")
        self.assertEqual(successful_retry.returncode, 0)
        delivered = self.state()
        self.assertTrue(delivered["delivered"])
        self.assertFalse(delivered["alertPending"])
        self.run_monitor("healthy")
        events = [call for call in self.calls() if call[:2] == ["system", "event"]]
        self.assertEqual(len(events), 2)

    def test_automatic_selection_targets_most_recent_eligible_main_session(self):
        self.run_monitor("gateway-fail")
        recovered = self.run_monitor("healthy")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        list_calls = [
            call for call in self.calls() if call[:3] == ["gateway", "call", "sessions.list"]
        ]
        self.assertEqual(len(list_calls), 2)
        params = json.loads(list_calls[-1][list_calls[-1].index("--params") + 1])
        self.assertEqual(params["agentId"], "main")
        self.assertTrue(params["requireLastInteraction"])
        self.assertEqual(params["sortBy"], "lastInteractionAt")
        event = next(call for call in self.calls() if call[:2] == ["system", "event"])
        self.assertEqual(event[event.index("--session-key") + 1], "agent:main:newest")

    def test_no_eligible_target_keeps_incident_pending_and_fails_loudly(self):
        self.run_monitor("gateway-fail")
        incident = self.state()["incidentId"]
        recovery = self.run_monitor("no-eligible")
        self.assertNotEqual(recovery.returncode, 0)
        self.assertIn("ALERT_PENDING NO_ELIGIBLE_SESSION", recovery.stderr)
        pending = self.state()
        self.assertFalse(pending["active"])
        self.assertEqual(pending["recoveredIncidentId"], incident)
        self.assertTrue(pending["alertPending"])
        self.assertFalse(pending["delivered"])
        self.assertEqual(pending["lastDeliveryFailure"], "NO_ELIGIBLE_SESSION")
        self.assertFalse(any(call[:2] == ["system", "event"] for call in self.calls()))

    def test_concurrent_recovery_processes_deliver_one_event(self):
        self.run_monitor("gateway-fail")
        env = {**self.env, "FAKE_MODE": "healthy", "FAKE_EVENT_DELAY": "0.4"}
        first = subprocess.Popen(
            ["node", str(MONITOR)], env=env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        second = subprocess.Popen(
            ["node", str(MONITOR)], env=env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        first_out, first_err = first.communicate(timeout=10)
        second_out, second_err = second.communicate(timeout=10)
        self.assertEqual(first.returncode, 0, first_err or first_out)
        self.assertEqual(second.returncode, 0, second_err or second_out)
        events = [call for call in self.calls() if call[:2] == ["system", "event"]]
        self.assertEqual(len(events), 1)
        self.assertTrue(self.state()["delivered"])

    def test_crashed_monitor_lease_is_reclaimed_and_pending_incident_delivers(self):
        self.run_monitor("gateway-fail")
        gate = self.root / "release-inspect"
        env = {
            **self.env,
            "FAKE_MODE": "healthy",
            "FAKE_INSPECT_GATE": str(gate),
            "LANE_GUARD_HEALTH_TIMEOUT_MS": "250",
            "LANE_GUARD_MONITOR_WAIT_MS": "500",
        }
        inspect_calls_before = sum(
            call[:2] == ["plugins", "inspect"] for call in self.calls()
        )
        crashed = subprocess.Popen(
            ["node", str(MONITOR)], env=env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            inspect_calls = sum(call[:2] == ["plugins", "inspect"] for call in self.calls())
            if inspect_calls > inspect_calls_before:
                break
            time.sleep(0.02)
        else:
            self.fail("crash probe never reached the protected monitor section")
        crashed.kill()
        crashed.communicate(timeout=5)
        gate.touch()
        time.sleep(3.5)
        recovered = self.run_monitor(
            "healthy",
            LANE_GUARD_HEALTH_TIMEOUT_MS="250",
            LANE_GUARD_MONITOR_WAIT_MS="1000",
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        events = [call for call in self.calls() if call[:2] == ["system", "event"]]
        self.assertEqual(len(events), 1)
        self.assertTrue(self.state()["delivered"])


if __name__ == "__main__":
    unittest.main()
