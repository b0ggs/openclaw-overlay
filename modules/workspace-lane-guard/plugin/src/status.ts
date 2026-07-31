import { SUPPORTED_OPENCLAW_PACKAGE } from "./lane-schema.ts";

export function boundedStatus(
  rows: Array<Record<string, unknown>>,
  limit = 50,
): Record<string, unknown> {
  return {
    schemaVersion: 1,
    qualifiedOpenclawPackage: {
      ...SUPPORTED_OPENCLAW_PACKAGE,
      enforcement: "exact-version-only",
    },
    truncated: rows.length > limit,
    reservations: rows.slice(0, limit).map((row) => ({
      authorityRoot: row.authority_root,
      access: row.access,
      state: row.state,
      targetAgentId: row.target_agent_id,
      nativeTaskId: row.native_task_id ?? null,
      runId: row.run_id ?? null,
      childSessionKey: row.child_session_key ?? null,
      updatedAt: row.updated_at,
      quarantineCode: row.quarantine_code ?? null,
    })),
  };
}
