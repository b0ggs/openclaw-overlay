import crypto from "node:crypto";
import type { GuardConfig, LaneRecord } from "./lane-schema.ts";
import {
  DEFAULT_LANE_TTL_MS,
  MAX_LANE_TTL_MS,
  PLUGIN_VERSION,
  boundedCode,
} from "./lane-schema.ts";
import {
  configFingerprint,
  readGlobalSubagentConfig,
  workspaceFingerprint,
} from "./fingerprint.ts";
import {
  canonicalWorkspace,
  rejectBroadWritableRoot,
  validateConfiguredRoots,
  validateTaskRoot,
} from "./path-policy.ts";
import type { ReservationStore } from "./reservation-db.ts";
import { boundedStatus } from "./status.ts";

type ForceConfirmation = {
  token: string;
  authorityKey: string;
  claimToken: string;
  expiresAt: number;
  nativeIds: Record<string, unknown>;
};

export function registerOperatorActions(
  api: any,
  config: GuardConfig,
  getStore: () => ReservationStore | null,
  readinessHandler: (ctx: any) => Promise<any>,
): void {
  const confirmations = new Map<string, ForceConfirmation>();

  api.session.controls.registerSessionAction({
    id: "prepareLane",
    description: "Validate and prepare a signed workspace lane record",
    requiredScopes: ["operator.admin"],
    schema: {
      type: "object",
      additionalProperties: false,
      required: ["parentAgentId", "targetAgentId", "taskRoot"],
      properties: {
        parentAgentId: { type: "string", minLength: 1, maxLength: 64 },
        targetAgentId: { type: "string", minLength: 1, maxLength: 64 },
        taskRoot: { type: "string", minLength: 1, maxLength: 4096 },
        ttlMs: { type: "integer", minimum: 60000, maximum: MAX_LANE_TTL_MS },
      },
    },
    handler: async (ctx: any) => {
      try {
        if (!ctx.sessionKey) throw new Error("SESSION_KEY_REQUIRED");
        const payload = ctx.payload as Record<string, unknown>;
        const target = config.targets.find((entry) => entry.agentId === payload.targetAgentId);
        if (!target) throw new Error("TARGET_NOT_ALLOWED");
        const roots = validateConfiguredRoots(config.targets);
        const root = roots.get(target.agentId)!;
        rejectBroadWritableRoot(root.root, target.access, process.env.OPENCLAW_WORKSPACE_ROOT);
        const taskRoot = validateTaskRoot(String(payload.taskRoot), root.root);
        const ttlMs = Number(payload.ttlMs ?? DEFAULT_LANE_TTL_MS);
        if (!Number.isInteger(ttlMs) || ttlMs < 60_000 || ttlMs > MAX_LANE_TTL_MS)
          throw new Error("LANE_TTL_INVALID");
        const issuedAt = Date.now();
        const lane: LaneRecord = {
          schemaVersion: 3,
          parentSessionKey: ctx.sessionKey,
          parentAgentId: String(payload.parentAgentId),
          targetAgentId: target.agentId,
          workspaceRoot: root.root,
          authorityRoot: root.root,
          taskRoot,
          access: target.access,
          issuedAt,
          expiresAt: issuedAt + ttlMs,
          issuerAuthority: "operator.admin",
          issuanceId: crypto.randomUUID(),
          configFingerprint: configFingerprint(config, roots, readGlobalSubagentConfig(api.config)),
          workspaceFingerprint: workspaceFingerprint(root),
          openclawVersion: config.openclawVersion,
          pluginVersion: PLUGIN_VERSION,
        };
        return { ok: true, result: { schemaVersion: 1, lane } };
      } catch (error) {
        return { ok: false, code: boundedCode(error), error: boundedCode(error) };
      }
    },
  });

  api.session.controls.registerSessionAction({
    id: "status",
    description: "Read bounded workspace lane reservation status",
    requiredScopes: ["operator.read"],
    schema: { type: "object", additionalProperties: false, properties: {} },
    handler: async () => {
      const store = getStore();
      if (!store) return { ok: false, code: "PLUGIN_NOT_READY", error: "PLUGIN_NOT_READY" };
      try {
        const rows = await store.list();
        return {
          ok: true,
          result: { ...boundedStatus(rows.reservations), barrierCount: rows.barriers.length },
        };
      } catch (error) {
        return { ok: false, code: boundedCode(error), error: boundedCode(error) };
      }
    },
  });

  api.session.controls.registerSessionAction({
    id: "beginClear",
    description: "Create a durable lane-closing barrier before session lane removal",
    requiredScopes: ["operator.admin"],
    schema: {
      type: "object",
      additionalProperties: false,
      required: ["lane"],
      properties: { lane: { type: "object" } },
    },
    handler: async (ctx: any) => {
      const store = getStore();
      if (!store || !ctx.sessionKey)
        return { ok: false, code: "PLUGIN_NOT_READY", error: "PLUGIN_NOT_READY" };
      try {
        const lane = (ctx.payload as Record<string, unknown>).lane as LaneRecord;
        if (lane.parentSessionKey !== ctx.sessionKey) throw new Error("LANE_PARENT_MISMATCH");
        const result = await store.beginClear(lane, ctx.sessionKey);
        if (!result.created) {
          return {
            ok: false,
            code: "LANE_HAS_ACTIVE_RESERVATION",
            error: "LANE_HAS_ACTIVE_RESERVATION",
            details: {
              conflictId:
                (result.conflict as Record<string, unknown> | undefined)?.conflictId ?? null,
              holderTaskId:
                (result.conflict as Record<string, unknown> | undefined)?.holderTaskId ?? null,
            },
          };
        }
        return {
          ok: true,
          result: {
            schemaVersion: 1,
            authorityKey: `sha256:${crypto.createHash("sha256").update(lane.authorityRoot).digest("hex")}`,
            barrierToken: result.barrierToken,
          },
        };
      } catch (error) {
        return { ok: false, code: boundedCode(error), error: boundedCode(error) };
      }
    },
  });

  api.session.controls.registerSessionAction({
    id: "completeClear",
    description: "Complete a lane clear after sessions.pluginPatch unset succeeds",
    requiredScopes: ["operator.admin"],
    schema: {
      type: "object",
      additionalProperties: false,
      required: ["authorityKey", "barrierToken"],
      properties: {
        authorityKey: { type: "string", minLength: 1 },
        barrierToken: { type: "string", minLength: 1 },
      },
    },
    handler: async (ctx: any) => {
      const store = getStore();
      if (!store || !ctx.sessionKey)
        return { ok: false, code: "PLUGIN_NOT_READY", error: "PLUGIN_NOT_READY" };
      try {
        const payload = ctx.payload as Record<string, string>;
        await store.completeClear(ctx.sessionKey, payload.authorityKey, payload.barrierToken);
        return { ok: true, result: { schemaVersion: 1, cleared: true } };
      } catch (error) {
        return { ok: false, code: boundedCode(error), error: boundedCode(error) };
      }
    },
  });

  api.session.controls.registerSessionAction({
    id: "forceClear",
    description: "Two-step operator-confirmed release of one ambiguous reservation",
    requiredScopes: ["operator.admin"],
    schema: {
      type: "object",
      additionalProperties: false,
      required: ["authorityRoot"],
      properties: {
        authorityRoot: { type: "string", minLength: 1 },
        confirmationToken: { type: "string", minLength: 1 },
      },
    },
    handler: async (ctx: any) => {
      const store = getStore();
      if (!store) return { ok: false, code: "PLUGIN_NOT_READY", error: "PLUGIN_NOT_READY" };
      try {
        const authorityRoot = canonicalWorkspace(String((ctx.payload as any).authorityRoot)).root;
        const snapshot = await store.list();
        const row = snapshot.reservations.find((entry) => entry.authority_root === authorityRoot);
        if (!row) throw new Error("RESERVATION_NOT_FOUND");
        const supplied = (ctx.payload as any).confirmationToken;
        if (!supplied) {
          const token = crypto.randomUUID();
          confirmations.set(token, {
            token,
            authorityKey: String(row.authority_key),
            claimToken: String(row.claim_token),
            expiresAt: Date.now() + 60_000,
            nativeIds: {
              nativeTaskId: row.native_task_id ?? null,
              runId: row.run_id ?? null,
              childSessionKey: row.child_session_key ?? null,
              state: row.state,
            },
          });
          return {
            ok: true,
            result: {
              schemaVersion: 1,
              confirmationRequired: true,
              confirmationToken: token,
              expiresAt: Date.now() + 60_000,
              ...confirmations.get(token)!.nativeIds,
            },
          };
        }
        const confirmation = confirmations.get(String(supplied));
        confirmations.delete(String(supplied));
        if (!confirmation || confirmation.expiresAt < Date.now())
          throw new Error("FORCE_CLEAR_CONFIRMATION_INVALID");
        if (
          confirmation.authorityKey !== row.authority_key ||
          confirmation.claimToken !== row.claim_token
        ) {
          throw new Error("FORCE_CLEAR_RESERVATION_CHANGED");
        }
        const result = await store.request("forceClear", {
          authorityKey: confirmation.authorityKey,
          claimToken: confirmation.claimToken,
        });
        if (result.changed !== 1) throw new Error("FORCE_CLEAR_LOST_OWNERSHIP");
        return {
          ok: true,
          result: { schemaVersion: 1, forceCleared: true, ...confirmation.nativeIds },
        };
      } catch (error) {
        return { ok: false, code: boundedCode(error), error: boundedCode(error) };
      }
    },
  });

  api.session.controls.registerSessionAction({
    id: "readiness",
    description: "Live Gateway-resident readiness challenge",
    requiredScopes: ["operator.read"],
    schema: {
      type: "object",
      additionalProperties: false,
      required: ["challenge"],
      properties: {
        challenge: { type: "string", minLength: 16, maxLength: 128, pattern: "^[A-Za-z0-9_-]+$" },
      },
    },
    handler: readinessHandler,
  });
}
