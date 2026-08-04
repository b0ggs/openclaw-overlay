#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { DatabaseSync } from "node:sqlite";

const PLUGIN_ID = "workspace-lane-guard";
const PLUGIN_VERSION = "0.1.0";
const POLICY_ID = "workspace-lane-admission";
const MAIN_AGENT_ID = process.env.LANE_GUARD_MAIN_AGENT_ID ?? "main";
const SESSION_PAGE_SIZE = 100;
const MAX_SESSION_PAGES = 5;
const stateDir = path.resolve(
  process.env.OPENCLAW_STATE_DIR ?? path.join(process.env.HOME ?? "", ".openclaw"),
);
const monitorDir = path.join(stateDir, "plugins", PLUGIN_ID, "health");
const statePath = path.join(monitorDir, "incident.json");
const leaseDbPath = path.join(monitorDir, "monitor-lease.sqlite");
const timeoutMs = Math.max(
  250,
  Math.min(10_000, Number(process.env.LANE_GUARD_HEALTH_TIMEOUT_MS ?? 3000)),
);
const minimumLeaseMs = timeoutMs * (MAX_SESSION_PAGES + 4) + 1000;
const leaseMs = Math.max(
  minimumLeaseMs,
  Math.min(180_000, Number(process.env.LANE_GUARD_MONITOR_LEASE_MS ?? minimumLeaseMs)),
);
const leaseWaitMs = Math.max(
  500,
  Math.min(30_000, Number(process.env.LANE_GUARD_MONITOR_WAIT_MS ?? timeoutMs * 4 + 1000)),
);
const leaseOwner = crypto.randomUUID();

class MonitorFailure extends Error {}

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
function sleepSync(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}
function ensureMonitorDir() {
  fs.mkdirSync(monitorDir, { recursive: true, mode: 0o700 });
  fs.chmodSync(monitorDir, 0o700);
}
function openLeaseDb() {
  ensureMonitorDir();
  const db = new DatabaseSync(leaseDbPath);
  fs.chmodSync(leaseDbPath, 0o600);
  db.exec(`PRAGMA busy_timeout = ${Math.max(500, Math.min(leaseWaitMs, 10_000))}`);
  db.exec(`
    CREATE TABLE IF NOT EXISTS monitor_lease (
      lease_name TEXT PRIMARY KEY,
      owner_token TEXT NOT NULL,
      expires_at INTEGER NOT NULL
    )
  `);
  return db;
}
function tryAcquireLease(db) {
  const now = Date.now();
  db.exec("BEGIN IMMEDIATE");
  try {
    db.prepare("DELETE FROM monitor_lease WHERE lease_name = ? AND expires_at <= ?").run(
      "healthmon",
      now,
    );
    db.prepare(
      "INSERT OR IGNORE INTO monitor_lease (lease_name, owner_token, expires_at) VALUES (?, ?, ?)",
    ).run("healthmon", leaseOwner, now + leaseMs);
    const row = db
      .prepare("SELECT owner_token FROM monitor_lease WHERE lease_name = ?")
      .get("healthmon");
    db.exec("COMMIT");
    return row?.owner_token === leaseOwner;
  } catch (error) {
    try {
      db.exec("ROLLBACK");
    } catch {}
    throw error;
  }
}
function acquireLease(db) {
  const deadline = Date.now() + leaseWaitMs;
  while (true) {
    try {
      if (tryAcquireLease(db)) return;
    } catch (error) {
      if (!String(error).includes("database is locked") || Date.now() >= deadline) throw error;
    }
    if (Date.now() >= deadline) throw new MonitorFailure("MONITOR_LEASE_BUSY");
    sleepSync(50);
  }
}
function releaseLease(db) {
  try {
    db.prepare("DELETE FROM monitor_lease WHERE lease_name = ? AND owner_token = ?").run(
      "healthmon",
      leaseOwner,
    );
  } catch {}
}
function atomicWrite(value) {
  ensureMonitorDir();
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
function gatewayResult(stdout) {
  const wire = parseJson(stdout);
  if (!wire || typeof wire !== "object") return null;
  return wire.result && typeof wire.result === "object" ? wire.result : wire;
}
function eligibleMainSession(row) {
  if (!row || typeof row !== "object") return false;
  if (typeof row.key !== "string" || !row.key.startsWith(`agent:${MAIN_AGENT_ID}:`)) return false;
  if (row.kind !== "direct" || row.archived === true || row.incognito === true) return false;
  if (row.visibility === "draft") return false;
  if (row.spawnedBy || row.parentSessionKey || row.controlOwnerSessionKey) return false;
  if (row.createdVia === "spawn" || row.createdVia === "cron" || row.createdVia === "hook")
    return false;
  return Number.isFinite(row.lastInteractionAt) && row.lastInteractionAt > 0;
}
function selectAlertSession() {
  if (!/^[a-z][a-z0-9_-]{0,63}$/.test(MAIN_AGENT_ID)) {
    return { sessionKey: null, failure: "INVALID_MAIN_AGENT_ID" };
  }
  for (let page = 0; page < MAX_SESSION_PAGES; page += 1) {
    const listed = cli([
      "gateway",
      "call",
      "sessions.list",
      "--json",
      "--timeout",
      String(timeoutMs),
      "--params",
      JSON.stringify({
        agentId: MAIN_AGENT_ID,
        archived: false,
        configuredAgentsOnly: true,
        requireLastInteraction: true,
        sortBy: "lastInteractionAt",
        limit: SESSION_PAGE_SIZE,
        offset: page * SESSION_PAGE_SIZE,
      }),
    ]);
    if (listed.status !== 0) return { sessionKey: null, failure: "SESSION_LIST_FAILED" };
    const result = gatewayResult(listed.stdout);
    if (!result || !Array.isArray(result.sessions)) {
      return { sessionKey: null, failure: "SESSION_LIST_INVALID" };
    }
    const eligible = result.sessions.filter(eligibleMainSession).sort((left, right) => {
      const byInteraction = right.lastInteractionAt - left.lastInteractionAt;
      return byInteraction || left.key.localeCompare(right.key);
    });
    if (eligible.length > 0) return { sessionKey: eligible[0].key, failure: null };
    if (result.hasMore !== true) return { sessionKey: null, failure: "NO_ELIGIBLE_SESSION" };
  }
  return { sessionKey: null, failure: "SESSION_LIST_BOUNDED_EXHAUSTION" };
}
function attemptAlertDelivery(incident, text) {
  if (incident.delivered === true) return incident;
  const attempted = {
    ...incident,
    alertPending: true,
    deliveryAttempts: Number(incident.deliveryAttempts ?? 0) + 1,
    lastDeliveryAttemptAt: Date.now(),
  };
  const selected = selectAlertSession();
  if (!selected.sessionKey) {
    attempted.delivered = false;
    attempted.alertPending = true;
    attempted.lastDeliveryFailure = selected.failure;
    atomicWrite(attempted);
    return attempted;
  }
  atomicWrite(attempted);
  const sent = cli([
    "system",
    "event",
    "--session-key",
    selected.sessionKey,
    "--mode",
    "now",
    "--text",
    text,
  ]);
  if (sent.status === 0) {
    attempted.delivered = true;
    attempted.alertPending = false;
    attempted.deliveredAt = Date.now();
    delete attempted.lastDeliveryFailure;
  } else {
    attempted.delivered = false;
    attempted.alertPending = true;
    attempted.lastDeliveryFailure = `SYSTEM_EVENT_EXIT_${sent.status ?? "UNKNOWN"}`;
  }
  atomicWrite(attempted);
  return attempted;
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
        alertPending: true,
        deliveryAttempts: 0,
        lastSuccessfulGeneration: previous.lastSuccessfulGeneration ?? null,
      };
  atomicWrite(incident);
  attemptAlertDelivery(
    incident,
    `Workspace lane guard unhealthy: ${code}; incident ${incident.incidentId}`,
  );
  throw new MonitorFailure(`UNHEALTHY ${code}`);
}

function runMonitor() {
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
  const pendingIncidentId = previous.active
    ? previous.incidentId
    : previous.alertPending === true
      ? previous.recoveredIncidentId
      : null;
  const pendingReasonCode = previous.active ? previous.reasonCode : previous.recoveredReasonCode;
  let alertState = previous;
  if (pendingIncidentId && previous.delivered !== true) {
    alertState = attemptAlertDelivery(
      {
        ...previous,
        incidentId: pendingIncidentId,
        reasonCode: pendingReasonCode,
        alertPending: true,
      },
      `Workspace lane guard recovered after ${pendingReasonCode ?? "UNKNOWN"}; incident ${pendingIncidentId}`,
    );
  }
  if (previous.active || previous.alertPending === true) {
    atomicWrite({
      schemaVersion: 1,
      active: false,
      recoveredIncidentId: pendingIncidentId,
      recoveredReasonCode: pendingReasonCode ?? null,
      recoveredAt: previous.active ? Date.now() : previous.recoveredAt,
      delivered: alertState.delivered === true,
      alertPending: alertState.delivered !== true,
      deliveryAttempts: Number(alertState.deliveryAttempts ?? 0),
      ...(alertState.lastDeliveryAttemptAt
        ? { lastDeliveryAttemptAt: alertState.lastDeliveryAttemptAt }
        : {}),
      ...(alertState.deliveredAt ? { deliveredAt: alertState.deliveredAt } : {}),
      ...(alertState.lastDeliveryFailure
        ? { lastDeliveryFailure: alertState.lastDeliveryFailure }
        : {}),
      lastSuccessfulGeneration: result.gatewayGenerationId,
    });
  } else {
    atomicWrite({
      ...(previous.recoveredIncidentId ? previous : { schemaVersion: 1, active: false }),
      lastSuccessfulAt: Date.now(),
      lastSuccessfulGeneration: result.gatewayGenerationId,
    });
  }
  if (alertState.delivered !== true && pendingIncidentId) {
    throw new MonitorFailure(
      `ALERT_PENDING ${alertState.lastDeliveryFailure ?? "DELIVERY_INCOMPLETE"}`,
    );
  }
  console.log(`HEALTHY ${result.gatewayGenerationId}`);
}

let leaseDb;
try {
  leaseDb = openLeaseDb();
  acquireLease(leaseDb);
  runMonitor();
} catch (error) {
  console.error(
    error instanceof MonitorFailure ? error.message : `MONITOR_FAILED ${String(error)}`,
  );
  process.exitCode = 1;
} finally {
  if (leaseDb) {
    releaseLease(leaseDb);
    leaseDb.close();
  }
}
