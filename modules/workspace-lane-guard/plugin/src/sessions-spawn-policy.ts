import type { GuardConfig, LaneRecord, TargetConfig } from "./lane-schema.ts";
import { boundedCode, parseLane, validateSpawnParams } from "./lane-schema.ts";
import {
  assertReviewedGlobalSubagentConfig,
  assertTargetConfigMatchesHost,
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
import type { ReservationClaim, ReservationStore } from "./reservation-db.ts";
import { attestSandbox } from "./sandbox-policy.ts";

export type PendingClaim = {
  key: string;
  requesterSessionKey: string;
  requesterAgentId: string;
  target: TargetConfig;
  lane: LaneRecord;
  claim: ReservationClaim;
  claimedAt: number;
  generationId: string;
  requestFingerprint: string;
  approvedParams: Record<string, unknown>;
};

type AdmissionDecision =
  | { params: Record<string, unknown> }
  | { block: true; blockReason: string };

type InFlightAdmission = {
  requesterSessionKey: string;
  requesterAgentId: string;
  issuanceId: string;
  generationId: string;
  requestFingerprint: string;
  promise: Promise<AdmissionDecision>;
};

export type AdmissionState = {
  generationId: string | null;
  policyRegistered: boolean;
  workerReady: boolean;
  reconcilerReady: boolean;
  store: ReservationStore | null;
  roots: Map<string, ReturnType<typeof canonicalWorkspace>>;
  configFingerprint: string;
  pending: Map<string, PendingClaim>;
  inFlight: Map<string, InFlightAdmission>;
};

function correlationKey(runId: string, toolCallId: string): string {
  return `${runId}\0${toolCallId}`;
}

function blockEnvelope(
  code: string,
  details: Record<string, unknown> = {},
): { block: true; blockReason: string } {
  const envelope = {
    schemaVersion: 1,
    code,
    ...details,
    guidance: code.includes("QUARANTINE")
      ? "Stop and request operator review."
      : "Use sessions_yield or select a separate workspace.",
  };
  return { block: true, blockReason: `LANE_CONFLICT ${JSON.stringify(envelope).slice(0, 900)}` };
}

export function pendingKey(runId: string, toolCallId: string): string {
  return correlationKey(runId, toolCallId);
}

function requestFingerprint(
  spawn: { task: string; taskName?: string; label?: string },
  lane: LaneRecord,
): string {
  return JSON.stringify([
    spawn.task,
    spawn.taskName ?? null,
    spawn.label ?? null,
    lane.schemaVersion,
    lane.parentSessionKey,
    lane.parentAgentId,
    lane.targetAgentId,
    lane.workspaceRoot,
    lane.authorityRoot,
    lane.taskRoot,
    lane.access,
    lane.issuedAt,
    lane.expiresAt,
    lane.issuerAuthority,
    lane.issuanceId,
    lane.configFingerprint,
    lane.workspaceFingerprint,
    lane.openclawVersion,
    lane.pluginVersion,
  ]);
}

function correlationMatches(
  admission: {
    requesterSessionKey: string;
    requesterAgentId: string;
    issuanceId: string;
    generationId: string;
    requestFingerprint: string;
  },
  expected: {
    requesterSessionKey: string;
    requesterAgentId: string;
    issuanceId: string;
    generationId: string;
    requestFingerprint: string;
  },
): boolean {
  return (
    admission.requesterSessionKey === expected.requesterSessionKey &&
    admission.requesterAgentId === expected.requesterAgentId &&
    admission.issuanceId === expected.issuanceId &&
    admission.generationId === expected.generationId &&
    admission.requestFingerprint === expected.requestFingerprint
  );
}

function copyDecision(decision: AdmissionDecision): AdmissionDecision {
  return "params" in decision ? { params: { ...decision.params } } : { ...decision };
}

export function createAdmissionPolicy(api: any, config: GuardConfig, state: AdmissionState) {
  return {
    id: "workspace-lane-admission",
    description:
      "Require authenticated workspace lanes and durable reservations for every native sessions_spawn",
    async evaluate(event: any, ctx: any) {
      if (event.toolName !== "sessions_spawn") return;
      try {
        if (
          !state.generationId ||
          !state.policyRegistered ||
          !state.workerReady ||
          !state.reconcilerReady ||
          !state.store
        ) {
          throw new Error("AUTHORITY_ROOT_QUARANTINE:PLUGIN_NOT_READY");
        }
        const generationId = state.generationId;
        const store = state.store;
        const sessionKey = ctx.sessionKey;
        const agentId = ctx.agentId;
        const runId = ctx.runId ?? event.runId;
        const toolCallId = ctx.toolCallId ?? event.toolCallId;
        if (
          !sessionKey ||
          !agentId ||
          !runId ||
          !toolCallId ||
          typeof ctx.getSessionExtension !== "function"
        ) {
          throw new Error("TRUSTED_TOOL_CONTEXT_MISSING");
        }
        const spawn = validateSpawnParams(event.params);
        const lane = parseLane(ctx.getSessionExtension("lane"));
        if (lane.parentSessionKey !== sessionKey || lane.parentAgentId !== agentId)
          throw new Error("LANE_PARENT_MISMATCH");
        const target = config.targets.find((entry) => entry.agentId === lane.targetAgentId);
        if (!target) throw new Error("LANE_TARGET_NOT_ALLOWED");
        if (lane.openclawVersion !== config.openclawVersion || lane.pluginVersion !== "0.1.0") {
          throw new Error("LANE_VERSION_DRIFT");
        }

        const key = correlationKey(runId, toolCallId);
        const fingerprint = requestFingerprint(spawn, lane);
        const correlation = {
          requesterSessionKey: sessionKey,
          requesterAgentId: agentId,
          issuanceId: lane.issuanceId,
          generationId,
          requestFingerprint: fingerprint,
        };
        const pending = state.pending.get(key);
        if (pending) {
          if (
            !correlationMatches(
              {
                requesterSessionKey: pending.requesterSessionKey,
                requesterAgentId: pending.requesterAgentId,
                issuanceId: pending.lane.issuanceId,
                generationId: pending.generationId,
                requestFingerprint: pending.requestFingerprint,
              },
              correlation,
            )
          ) {
            throw new Error("CORRELATION_REUSE_MISMATCH");
          }
          return { params: { ...pending.approvedParams } };
        }
        const inFlight = state.inFlight.get(key);
        if (inFlight) {
          if (!correlationMatches(inFlight, correlation)) {
            throw new Error("CORRELATION_REUSE_MISMATCH");
          }
          return copyDecision(await inFlight.promise);
        }

        let claim: ReservationClaim | null = null;
        const evaluation = (async (): Promise<AdmissionDecision> => {
          try {
            assertReviewedGlobalSubagentConfig(api.config);
            const roots = validateConfiguredRoots(config.targets);
            assertTargetConfigMatchesHost(api.config, config.targets, roots);
            const root = roots.get(target.agentId)!;
            rejectBroadWritableRoot(root.root, target.access, process.env.OPENCLAW_WORKSPACE_ROOT);
            if (
              lane.workspaceRoot !== root.root ||
              lane.authorityRoot !== root.root ||
              lane.access !== target.access
            )
              throw new Error("LANE_TARGET_CONFIG_MISMATCH");
            validateTaskRoot(lane.taskRoot, root.root);
            const currentConfigFingerprint = configFingerprint(
              config,
              roots,
              readGlobalSubagentConfig(api.config),
            );
            if (
              lane.configFingerprint !== currentConfigFingerprint ||
              state.configFingerprint !== currentConfigFingerprint
            ) {
              throw new Error("GLOBAL_SUBAGENT_CONFIG_DRIFT");
            }
            if (lane.workspaceFingerprint !== workspaceFingerprint(root))
              throw new Error("WORKSPACE_FINGERPRINT_DRIFT");

            await attestSandbox(
              api,
              target,
              root.root,
              `${sessionKey}:lane-probe:${lane.issuanceId}`,
            );

            const finalRoots = validateConfiguredRoots(config.targets);
            assertTargetConfigMatchesHost(api.config, config.targets, finalRoots);
            const finalRoot = finalRoots.get(target.agentId)!;
            validateTaskRoot(lane.taskRoot, finalRoot.root);
            if (workspaceFingerprint(finalRoot) !== lane.workspaceFingerprint)
              throw new Error("WORKSPACE_FINGERPRINT_DRIFT");
            if (
              configFingerprint(config, finalRoots, readGlobalSubagentConfig(api.config)) !==
              lane.configFingerprint
            ) {
              throw new Error("GLOBAL_SUBAGENT_CONFIG_DRIFT");
            }
            if (
              state.generationId !== generationId ||
              state.store !== store ||
              !state.workerReady ||
              !state.reconcilerReady
            ) {
              throw new Error("PLUGIN_GENERATION_DRIFT");
            }

            const acquired = await store.acquire(lane);
            if ("conflict" in acquired) {
              const conflictDetails: Record<string, unknown> = {
                authorityRoot: acquired.conflict.authorityRoot,
                conflictId: acquired.conflict.conflictId,
              };
              if (
                acquired.conflict.sameControllingSession === true &&
                typeof acquired.conflict.holderTaskId === "string"
              ) {
                conflictDetails.holderTaskId = acquired.conflict.holderTaskId;
              }
              return blockEnvelope(String(acquired.conflict.code), conflictDetails);
            }
            claim = acquired;
            const approvedParams = {
              task: spawn.task,
              ...(spawn.taskName ? { taskName: spawn.taskName } : {}),
              ...(spawn.label ? { label: spawn.label } : {}),
              agentId: target.agentId,
              cwd: root.root,
              runtime: "subagent",
              mode: "run",
              cleanup: "delete",
              sandbox: "require",
            };
            state.pending.set(key, {
              key,
              requesterSessionKey: sessionKey,
              requesterAgentId: agentId,
              target,
              lane,
              claim,
              claimedAt: Date.now(),
              generationId,
              requestFingerprint: fingerprint,
              approvedParams,
            });
            return { params: { ...approvedParams } };
          } catch (error) {
            const code = boundedCode(error, "ADMISSION_REJECTED");
            if (claim) {
              try {
                await store.quarantine(claim, code);
              } catch {}
            }
            return blockEnvelope(code);
          }
        })();
        const admission: InFlightAdmission = { ...correlation, promise: evaluation };
        state.inFlight.set(key, admission);
        try {
          return copyDecision(await evaluation);
        } finally {
          if (state.inFlight.get(key) === admission) state.inFlight.delete(key);
        }
      } catch (error) {
        return blockEnvelope(boundedCode(error, "ADMISSION_REJECTED"));
      }
    },
  };
}
