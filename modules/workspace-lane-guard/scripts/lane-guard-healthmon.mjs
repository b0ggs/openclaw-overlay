#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

const PLUGIN_ID = "workspace-lane-guard";
const PLUGIN_VERSION = "0.1.0";
const POLICY_ID = "workspace-lane-admission";
const stateDir = path.resolve(
  process.env.OPENCLAW_STATE_DIR ?? path.join(process.env.HOME ?? "", ".openclaw"),
);
const monitorDir = path.join(stateDir, "plugins", PLUGIN_ID, "health");
const statePath = path.join(monitorDir, "incident.json");
const timeoutMs = Math.max(
  250,
  Math.min(10_000, Number(process.env.LANE_GUARD_HEALTH_TIMEOUT_MS ?? 3000)),
);

function cli(args) {
  return spawnSync("openclaw", args, {
    encoding: "utf8",
    timeout: timeoutMs,
    env: { ...process.env, OPENCLAW_STATE_DIR: stateDir },
    stdio: ["ignore", "pipe", "pipe"],
  });
}
function parseJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}
function atomicWrite(value) {
  fs.mkdirSync(monitorDir, { recursive: true, mode: 0o700 });
  fs.chmodSync(monitorDir, 0o700);
  const temp = `${statePath}.${process.pid}.tmp`;
  fs.writeFileSync(temp, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
  fs.renameSync(temp, statePath);
}
function readState() {
  try {
    return parseJson(fs.readFileSync(statePath, "utf8")) ?? {};
  } catch {
    return {};
  }
}
function includesContract(value, needle) {
  if (value === needle) return true;
  if (Array.isArray(value)) return value.some((item) => includesContract(item, needle));
  if (value && typeof value === "object")
    return Object.values(value).some((item) => includesContract(item, needle));
  return false;
}
function unhealthy(code, previous) {
  const now = Date.now();
  const incident = previous.active
    ? { ...previous, lastObservedAt: now, reasonCode: code }
    : {
        schemaVersion: 1,
        active: true,
        incidentId: crypto.randomUUID(),
        firstObservedAt: now,
        lastObservedAt: now,
        reasonCode: code,
        delivered: false,
        lastSuccessfulGeneration: previous.lastSuccessfulGeneration ?? null,
      };
  atomicWrite(incident);
  const sessionKey = process.env.LANE_GUARD_ALERT_SESSION_KEY;
  if (sessionKey && !incident.delivered) {
    const sent = cli([
      "system",
      "event",
      "--session-key",
      sessionKey,
      "--mode",
      "now",
      "--text",
      `Workspace lane guard unhealthy: ${code}; incident ${incident.incidentId}`,
    ]);
    if (sent.status === 0) {
      incident.delivered = true;
      atomicWrite(incident);
    }
  }
  console.error(`UNHEALTHY ${code}`);
  process.exit(1);
}

const previous = readState();
const cold = cli(["plugins", "inspect", PLUGIN_ID, "--json"]);
if (cold.status !== 0) unhealthy("COLD_INSPECT_FAILED", previous);
const coldJson = parseJson(cold.stdout);
if (
  !coldJson ||
  !includesContract(coldJson, PLUGIN_ID) ||
  !includesContract(coldJson, PLUGIN_VERSION) ||
  !includesContract(coldJson, POLICY_ID)
)
  unhealthy("COLD_CONTRACT_MISMATCH", previous);

const challenge = crypto.randomBytes(24).toString("base64url");
const rpcParams = JSON.stringify({
  pluginId: PLUGIN_ID,
  actionId: "readiness",
  sessionKey: process.env.LANE_GUARD_READINESS_SESSION_KEY ?? "agent:main:main",
  payload: { challenge },
});
const live = cli([
  "gateway",
  "call",
  "plugins.sessionAction",
  "--json",
  "--timeout",
  String(timeoutMs),
  "--params",
  rpcParams,
]);
if (live.status !== 0) unhealthy("LIVE_RPC_FAILED", previous);
const wire = parseJson(live.stdout);
const result = wire?.result?.result ?? wire?.result ?? wire;
if (
  !result ||
  result.ready !== true ||
  result.challenge !== challenge ||
  result.pluginId !== PLUGIN_ID ||
  result.pluginVersion !== PLUGIN_VERSION ||
  result.policyId !== POLICY_ID ||
  result.policyRegistered !== true ||
  result.workerReady !== true ||
  result.reconcilerReady !== true ||
  typeof result.gatewayGenerationId !== "string" ||
  result.gatewayGenerationId.length < 8 ||
  !(result.lastReconciledAt >= result.gatewayStartedAt && result.gatewayStartedAt > 0)
) {
  unhealthy("LIVE_READINESS_MISMATCH", previous);
}
if (previous.active) {
  atomicWrite({
    schemaVersion: 1,
    active: false,
    recoveredIncidentId: previous.incidentId,
    recoveredAt: Date.now(),
    delivered: previous.delivered === true,
    lastSuccessfulGeneration: result.gatewayGenerationId,
  });
} else {
  atomicWrite({
    schemaVersion: 1,
    active: false,
    lastSuccessfulAt: Date.now(),
    lastSuccessfulGeneration: result.gatewayGenerationId,
  });
}
console.log(`HEALTHY ${result.gatewayGenerationId}`);
