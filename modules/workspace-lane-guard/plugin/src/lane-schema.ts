export const PLUGIN_ID = "workspace-lane-guard";
export const PLUGIN_VERSION = "0.1.0";
export const POLICY_ID = "workspace-lane-admission";
export const SUPPORTED_OPENCLAW_VERSION = "2026.7.2-beta.4";
export const SUPPORTED_OPENCLAW_PACKAGE = Object.freeze({
  version: SUPPORTED_OPENCLAW_VERSION,
  npmShasum: "95ed4f87ce8e8500e0474e07d0fa1e79616a2055",
  npmIntegrity:
    "sha512-Wqk1avvuJAnJWESA+EJdCObj9i4sWYf5hGczKAs00gRcHFJ/XUlXUbHPIJ7WPKexKMN31ybP0fcVVsROFMzOgA==",
  tarballUrl: "https://registry.npmjs.org/openclaw/-/openclaw-2026.7.2-beta.4.tgz",
  tarballSha256: "822b3e5cec8bd41a7d2f4ff1709f1e9e789c6e5e4e23e058443b22f8f6e07ead",
  buildCommit: "5e63b365d4d3e62ef600b783fad7c5043b6f4738",
});
export const LANE_SCHEMA_VERSION = 3;
export const DEFAULT_LANE_TTL_MS = 8 * 60 * 60 * 1000;
export const MAX_LANE_TTL_MS = 24 * 60 * 60 * 1000;
export const MAX_TASK_BYTES = 64 * 1024;
export const MAX_LABEL_BYTES = 256;
export const TASK_NAME_PATTERN = /^[a-z][a-z0-9_-]{0,63}$/;
const FORBIDDEN_CHILD_TOOLS = new Set([
  "sessions_spawn",
  "sessions_send",
  "sessions_list",
  "sessions_history",
  "subagents",
  "gateway",
  "cron",
  "session_status",
  "nodes",
  "node_exec",
  "node_process",
]);

export type LaneAccess = "ro" | "rw";

export type TargetConfig = {
  agentId: string;
  workspaceRoot: string;
  access: LaneAccess;
  tools: string[];
  model: string;
  thinking: string;
  sameRootPair?: string;
};

export type GuardConfig = {
  stateDir: string;
  openclawVersion: string;
  acquisitionDeadlineMs: number;
  targets: TargetConfig[];
};

export type LaneRecord = {
  schemaVersion: 3;
  parentSessionKey: string;
  parentAgentId: string;
  targetAgentId: string;
  workspaceRoot: string;
  authorityRoot: string;
  taskRoot: string;
  access: LaneAccess;
  issuedAt: number;
  expiresAt: number;
  issuerAuthority: "operator.admin";
  issuanceId: string;
  configFingerprint: string;
  workspaceFingerprint: string;
  openclawVersion: string;
  pluginVersion: string;
};

export type SpawnInput = {
  task: string;
  taskName?: string;
  label?: string;
};

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requiredString(value: unknown, field: string, max = 4096): string {
  if (typeof value !== "string" || value.length === 0 || Buffer.byteLength(value) > max) {
    throw new Error(`INVALID_${field.toUpperCase()}`);
  }
  return value;
}

export function parseGuardConfig(value: unknown): GuardConfig {
  if (!isObject(value)) throw new Error("INVALID_PLUGIN_CONFIG");
  const allowed = new Set(["stateDir", "openclawVersion", "acquisitionDeadlineMs", "targets"]);
  for (const key of Object.keys(value))
    if (!allowed.has(key)) throw new Error(`UNKNOWN_CONFIG_KEY:${key}`);
  const stateDir = requiredString(value.stateDir, "stateDir");
  const openclawVersion = requiredString(value.openclawVersion, "openclawVersion", 128);
  if (openclawVersion !== SUPPORTED_OPENCLAW_VERSION) {
    throw new Error("UNSUPPORTED_OPENCLAW_VERSION");
  }
  const acquisitionDeadlineMs =
    typeof value.acquisitionDeadlineMs === "number" ? value.acquisitionDeadlineMs : 500;
  if (
    !Number.isInteger(acquisitionDeadlineMs) ||
    acquisitionDeadlineMs < 50 ||
    acquisitionDeadlineMs > 500
  ) {
    throw new Error("INVALID_ACQUISITION_DEADLINE");
  }
  if (!Array.isArray(value.targets) || value.targets.length < 1 || value.targets.length > 32) {
    throw new Error("INVALID_TARGETS");
  }
  const targets = value.targets.map((entry, index): TargetConfig => {
    if (!isObject(entry)) throw new Error(`INVALID_TARGET:${index}`);
    const targetAllowed = new Set([
      "agentId",
      "workspaceRoot",
      "access",
      "tools",
      "model",
      "thinking",
      "sameRootPair",
    ]);
    for (const key of Object.keys(entry))
      if (!targetAllowed.has(key)) throw new Error(`UNKNOWN_TARGET_KEY:${index}:${key}`);
    const agentId = requiredString(entry.agentId, "agentId", 64);
    if (!TASK_NAME_PATTERN.test(agentId)) throw new Error(`INVALID_AGENT_ID:${index}`);
    const workspaceRoot = requiredString(entry.workspaceRoot, "workspaceRoot");
    if (entry.access !== "ro" && entry.access !== "rw") throw new Error(`INVALID_ACCESS:${index}`);
    if (
      !Array.isArray(entry.tools) ||
      entry.tools.length === 0 ||
      entry.tools.some((tool) => typeof tool !== "string" || !tool)
    ) {
      throw new Error(`INVALID_TOOLS:${index}`);
    }
    if (new Set(entry.tools).size !== entry.tools.length)
      throw new Error(`DUPLICATE_TOOLS:${index}`);
    if (entry.tools.some((tool) => FORBIDDEN_CHILD_TOOLS.has(String(tool)))) {
      throw new Error(`FORBIDDEN_CHILD_TOOL:${index}`);
    }
    const sameRootPair =
      entry.sameRootPair === undefined
        ? undefined
        : requiredString(entry.sameRootPair, "sameRootPair", 128);
    return {
      agentId,
      workspaceRoot,
      access: entry.access,
      tools: [...entry.tools],
      model: requiredString(entry.model, "model", 256),
      thinking: requiredString(entry.thinking, "thinking", 64),
      sameRootPair,
    };
  });
  if (new Set(targets.map((target) => target.agentId)).size !== targets.length) {
    throw new Error("DUPLICATE_AGENT_ID");
  }
  return { stateDir, openclawVersion, acquisitionDeadlineMs, targets };
}

export function parseLane(value: unknown, now = Date.now()): LaneRecord {
  if (!isObject(value)) throw new Error("LANE_MISSING");
  const expected = [
    "schemaVersion",
    "parentSessionKey",
    "parentAgentId",
    "targetAgentId",
    "workspaceRoot",
    "authorityRoot",
    "taskRoot",
    "access",
    "issuedAt",
    "expiresAt",
    "issuerAuthority",
    "issuanceId",
    "configFingerprint",
    "workspaceFingerprint",
    "openclawVersion",
    "pluginVersion",
  ];
  if (Object.keys(value).length !== expected.length || expected.some((key) => !(key in value))) {
    throw new Error("LANE_SCHEMA_MISMATCH");
  }
  if (value.schemaVersion !== LANE_SCHEMA_VERSION) throw new Error("LANE_SCHEMA_MISMATCH");
  if (value.access !== "ro" && value.access !== "rw") throw new Error("LANE_ACCESS_INVALID");
  if (value.issuerAuthority !== "operator.admin") throw new Error("LANE_ISSUER_INVALID");
  if (!Number.isSafeInteger(value.issuedAt) || !Number.isSafeInteger(value.expiresAt))
    throw new Error("LANE_TIME_INVALID");
  const issuedAt = value.issuedAt as number;
  const expiresAt = value.expiresAt as number;
  if (expiresAt <= issuedAt || expiresAt - issuedAt > MAX_LANE_TTL_MS)
    throw new Error("LANE_TTL_INVALID");
  if (expiresAt <= now) throw new Error("LANE_EXPIRED");
  for (const field of expected.filter(
    (key) => !["schemaVersion", "issuedAt", "expiresAt", "access"].includes(key),
  )) {
    requiredString(value[field], field);
  }
  return value as LaneRecord;
}

export function validateSpawnParams(params: Record<string, unknown>): SpawnInput {
  const allowed = new Set([
    "task",
    "taskName",
    "label",
    "agentId",
    "cwd",
    "runtime",
    "mode",
    "cleanup",
    "sandbox",
  ]);
  for (const key of Object.keys(params)) {
    if (!allowed.has(key)) throw new Error(`UNSUPPORTED_SPAWN_KEY:${key}`);
  }
  for (const forbidden of ["agentId", "cwd", "runtime", "mode", "cleanup", "sandbox"]) {
    if (forbidden in params) throw new Error(`CALLER_OVERRIDE_REJECTED:${forbidden}`);
  }
  const task = requiredString(params.task, "task", MAX_TASK_BYTES);
  const taskName =
    params.taskName === undefined ? undefined : requiredString(params.taskName, "taskName", 64);
  if (
    taskName !== undefined &&
    (!TASK_NAME_PATTERN.test(taskName) || taskName === "last" || taskName === "all")
  ) {
    throw new Error("INVALID_TASK_NAME");
  }
  const label =
    params.label === undefined ? undefined : requiredString(params.label, "label", MAX_LABEL_BYTES);
  return { task, taskName, label };
}

export function boundedCode(error: unknown, fallback = "INTERNAL_ERROR"): string {
  const raw = error instanceof Error ? error.message : String(error ?? fallback);
  const normalized = raw.replace(/[^A-Z0-9:_-]/gi, "_").slice(0, 160);
  return normalized || fallback;
}
