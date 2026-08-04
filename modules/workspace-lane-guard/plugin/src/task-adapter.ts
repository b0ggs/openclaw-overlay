const TERMINAL = new Set([
  "succeeded",
  "failed",
  "timed-out",
  "timed_out",
  "cancelled",
  "canceled",
]);
const ACTIVE = new Set(["queued", "running"]);

export type NativeTask = {
  id: string;
  runtime: string;
  sessionKey: string;
  childSessionKey?: string;
  agentId?: string;
  runId?: string;
  status: string;
  createdAt: number;
};

export function isTerminalTask(task: NativeTask): boolean {
  return TERMINAL.has(task.status);
}

export function isActiveTask(task: NativeTask): boolean {
  return ACTIVE.has(task.status);
}

function findAccepted(
  value: unknown,
  depth = 0,
): { runId: string; childSessionKey: string } | null {
  if (depth > 8 || value === null || value === undefined) return null;
  if (typeof value === "string") {
    if (value.length > 128 * 1024) return null;
    try {
      return findAccepted(JSON.parse(value), depth + 1);
    } catch {
      return null;
    }
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = findAccepted(item, depth + 1);
      if (found) return found;
    }
    return null;
  }
  if (typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  if (typeof record.runId === "string" && typeof record.childSessionKey === "string") {
    return { runId: record.runId, childSessionKey: record.childSessionKey };
  }
  for (const key of ["result", "details", "content", "text", "data", "value"]) {
    if (key in record) {
      const found = findAccepted(record[key], depth + 1);
      if (found) return found;
    }
  }
  return null;
}

export function parseAcceptedSpawnResult(
  result: unknown,
): { runId: string; childSessionKey: string } | null {
  return findAccepted(result);
}

export function listRequesterTasks(api: any, requesterSessionKey: string): NativeTask[] {
  return api.runtime.tasks.runs
    .bindSession({ sessionKey: requesterSessionKey })
    .list() as NativeTask[];
}

export function resolveMatchingNativeTask(
  api: any,
  requesterSessionKey: string,
  accepted: { runId: string; childSessionKey: string },
  expected: { targetAgentId: string; claimedAt: number },
): NativeTask {
  const tasks = listRequesterTasks(api, requesterSessionKey);
  const matches = tasks.filter(
    (task) =>
      task.runtime === "subagent" &&
      task.runId === accepted.runId &&
      task.childSessionKey === accepted.childSessionKey &&
      task.agentId === expected.targetAgentId &&
      task.sessionKey === requesterSessionKey &&
      task.createdAt >= expected.claimedAt - 5_000 &&
      task.createdAt <= Date.now() + 5_000,
  );
  if (matches.length !== 1)
    throw new Error(matches.length === 0 ? "NATIVE_TASK_NOT_FOUND" : "NATIVE_TASK_AMBIGUOUS");
  return matches[0];
}

export function resolveTaskForReservation(
  api: any,
  row: Record<string, unknown>,
): NativeTask | null {
  const tasks = listRequesterTasks(api, String(row.owner_session_key));
  const matches = tasks.filter(
    (task) =>
      task.runtime === "subagent" &&
      task.id === row.native_task_id &&
      task.runId === row.run_id &&
      task.childSessionKey === row.child_session_key &&
      task.agentId === row.target_agent_id &&
      task.sessionKey === row.owner_session_key,
  );
  if (matches.length > 1) throw new Error("NATIVE_TASK_AMBIGUOUS");
  return matches[0] ?? null;
}
