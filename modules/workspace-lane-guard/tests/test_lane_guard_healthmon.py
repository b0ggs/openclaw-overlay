import json
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest


MODULE = pathlib.Path(__file__).resolve().parents[1]
MONITOR = MODULE / "scripts/lane-guard-healthmon.mjs"


FAKE = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
log = pathlib.Path(os.environ["FAKE_LOG"])
with log.open("a", encoding="utf-8") as out:
    out.write(json.dumps(args) + "\n")
mode = os.environ.get("FAKE_MODE", "healthy")
if args[:2] == ["plugins", "inspect"]:
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

    def run_monitor(self, mode="healthy", alert=False):
        env = {**self.env, "FAKE_MODE": mode}
        if alert:
            env["LANE_GUARD_ALERT_SESSION_KEY"] = "agent:main:main"
        return subprocess.run(["node", str(MONITOR)], env=env, text=True, capture_output=True)

    def state(self):
        path = self.root / "state/plugins/workspace-lane-guard/health/incident.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def calls(self):
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
        self.run_monitor("gateway-fail", alert=True)
        failed = self.state()
        self.assertTrue(failed["active"])
        result = self.run_monitor("healthy", alert=True)
        self.assertEqual(result.returncode, 0)
        recovered = self.state()
        self.assertFalse(recovered["active"])
        self.assertEqual(recovered["recoveredIncidentId"], failed["incidentId"])


if __name__ == "__main__":
    unittest.main()
