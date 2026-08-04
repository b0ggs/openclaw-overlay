#!/usr/bin/env node
import { spawnSync } from "node:child_process";

const [actionId, sessionKey, payloadText = "{}"] = process.argv.slice(2);
const allowed = new Set([
  "prepareLane",
  "status",
  "beginClear",
  "completeClear",
  "forceClear",
  "readiness",
]);
if (!allowed.has(actionId) || !sessionKey) {
  console.error(
    "usage: workspace-lane-control.mjs <prepareLane|status|beginClear|completeClear|forceClear|readiness> <session-key> [payload-json]",
  );
  process.exit(2);
}
let payload;
try {
  payload = JSON.parse(payloadText);
} catch {
  console.error("payload must be valid JSON");
  process.exit(2);
}
if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
  console.error("payload must be a JSON object");
  process.exit(2);
}
const params = JSON.stringify({
  pluginId: "workspace-lane-guard",
  actionId,
  sessionKey,
  payload,
});
const result = spawnSync(
  "openclaw",
  [
    "gateway",
    "call",
    "plugins.sessionAction",
    "--json",
    "--timeout",
    process.env.LANE_GUARD_RPC_TIMEOUT_MS ?? "3000",
    "--params",
    params,
  ],
  { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], env: process.env },
);
if (result.stdout) process.stdout.write(result.stdout);
if (result.stderr) process.stderr.write(result.stderr);
process.exit(result.status ?? 1);
