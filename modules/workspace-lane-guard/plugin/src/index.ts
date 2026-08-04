import crypto from "node:crypto";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { parseGuardConfig, boundedCode } from "./lane-schema.ts";
import {
  assertReviewedGlobalSubagentConfig,
  assertTargetConfigMatchesHost,
  assertTrustedToolPolicyIsolation,
  configFingerprint,
  readGlobalSubagentConfig,
} from "./fingerprint.ts";
import { validateConfiguredRoots } from "./path-policy.ts";
import { registerLaneExtension } from "./lane-session-extension.ts";
import { ReservationStore } from "./reservation-db.ts";
import { createReconcilerState, reconcileReservations } from "./reservation-reconciler.ts";
import {
  createAdmissionPolicy,
  pendingKey,
  type AdmissionState,
  type PendingClaim,
} from "./sessions-spawn-policy.ts";
import {
  listRequesterTasks,
  parseAcceptedSpawnResult,
  resolveMatchingNativeTask,
  resolveTaskForReservation,
  isTerminalTask,
} from "./task-adapter.ts";
import { createReadinessHandler, type RuntimeReadiness } from "./readiness.ts";
import { registerOperatorActions } from "./operator-actions.ts";

type SharedRuntime = {
  readiness: RuntimeReadiness;
  admission: AdmissionState;
  reconciler: ReturnType<typeof createReconcilerState>;
  reconcileTimer: NodeJS.Timeout | null;
  startPromise: Promise<void> | null;
  owners: Set<string>;
};

const RUNTIME_KEY = Symbol.for("openclaw.workspace-lane-guard.runtime.v1");

export default definePluginEntry({
  id: "workspace-lane-guard",
  name: "Workspace Lane Guard",
  description:
    "Authenticated workspace lanes and durable hierarchical reservations for native subagents",
  register(api) {
    const config = parseGuardConfig(api.pluginConfig);
    assertReviewedGlobalSubagentConfig(api.config);
    assertTrustedToolPolicyIsolation(api.config);
    const roots = validateConfiguredRoots(config.targets);
    assertTargetConfigMatchesHost(api.config, config.targets, roots);
    const fingerprint = configFingerprint(config, roots, readGlobalSubagentConfig(api.config));
    const globals = globalThis as any;
    if (!globals[RUNTIME_KEY]) {
      const reconciler = createReconcilerState();
      globals[RUNTIME_KEY] = {
        reconciler,
        readiness: {
          gatewayGenerationId: null,
          gatewayStartedAt: 0,
          policyRegistered: false,
          workerReady: false,
          reconciler,
          store: null,
        },
        admission: {
          generationId: null,
          policyRegistered: false,
          workerReady: false,
          reconcilerReady: false,
          store: null,
          roots,
          configFingerprint: fingerprint,
          pending: new Map(),
          inFlight: new Map(),
        },
        reconcileTimer: null,
        startPromise: null,
        owners: new Set(),
      };
    }
    const shared = globals[RUNTIME_KEY] as SharedRuntime;
    if (shared.admission.configFingerprint !== fingerprint)
      throw new Error("GLOBAL_SUBAGENT_CONFIG_DRIFT");
    const { reconciler, readiness, admission } = shared;
    const instanceId = crypto.randomUUID();

    async function runReconciliation(): Promise<void> {
      const store = readiness.store;
      const generationId = readiness.gatewayGenerationId;
      if (!store || !generationId) return;
      try {
        await reconcileReservations(api, store, generationId, reconciler);
      } catch (error) {
        api.logger.error(`workspace-lane reconciliation failed: ${boundedCode(error)}`);
      } finally {
        admission.reconcilerReady =
          readiness.store === store &&
          readiness.gatewayGenerationId === generationId &&
          reconciler.ready &&
          reconciler.generationId === generationId;
      }
    }

    async function quarantinePending(pending: PendingClaim, code: string): Promise<void> {
      if (!readiness.store) return;
      try {
        await readiness.store.quarantine(pending.claim, code);
      } catch (error) {
        api.logger.error(`workspace-lane quarantine failed: ${boundedCode(error)}`);
      }
    }

    async function handleAfterSpawnTool(event: any, ctx: any): Promise<void> {
      if (event.toolName !== "sessions_spawn") return;
      const runId = ctx.runId ?? event.runId;
      const toolCallId = ctx.toolCallId ?? event.toolCallId;
      if (!runId || !toolCallId) return;
      const key = pendingKey(runId, toolCallId);
      const pending = admission.pending.get(key);
      if (!pending || !readiness.store) return;
      admission.pending.delete(key);

      const accepted = parseAcceptedSpawnResult(event.result);
      if (!accepted) {
        if (event.error) {
          await new Promise((resolve) => setTimeout(resolve, 150));
          const possible = listRequesterTasks(api, pending.requesterSessionKey).filter(
            (task) =>
              task.runtime === "subagent" &&
              task.agentId === pending.target.agentId &&
              task.createdAt >= pending.claimedAt - 5_000,
          );
          if (possible.length === 0) {
            try {
              await readiness.store.release({
                authority_key: pending.claim.authorityKey,
                claim_token: pending.claim.claimToken,
              });
              return;
            } catch {}
          }
        }
        await quarantinePending(pending, "POST_TOOL_ACCEPTANCE_AMBIGUOUS");
        return;
      }

      let task = null;
      for (let attempt = 0; attempt < 5; attempt += 1) {
        try {
          task = resolveMatchingNativeTask(api, pending.requesterSessionKey, accepted, {
            targetAgentId: pending.target.agentId,
            claimedAt: pending.claimedAt,
          });
          break;
        } catch {
          if (attempt < 4) await new Promise((resolve) => setTimeout(resolve, 50 + attempt * 25));
        }
      }
      if (!task) {
        await quarantinePending(pending, "NATIVE_TASK_BINDING_UNPROVEN");
        return;
      }
      try {
        await readiness.store.bind(pending.claim, {
          id: task.id,
          runId: accepted.runId,
          childSessionKey: accepted.childSessionKey,
        });
      } catch {
        await quarantinePending(pending, "NATIVE_TASK_BIND_LOST_OWNERSHIP");
      }
    }

    registerLaneExtension(api, async (sessionKey, reason) => {
      if (!sessionKey || !readiness.store) return;
      try {
        const snapshot = await readiness.store.request("getByOwner", {
          ownerSessionKey: sessionKey,
        });
        for (const row of snapshot) {
          await readiness.store.quarantine(
            row,
            `SESSION_${String(reason).toUpperCase()}_WITH_RESERVATION`,
          );
        }
      } catch (error) {
        api.logger.error(`workspace-lane session cleanup quarantine failed: ${boundedCode(error)}`);
      }
    });

    const policy = createAdmissionPolicy(api, config, admission);
    api.registerTrustedToolPolicy(policy);
    readiness.policyRegistered = true;
    admission.policyRegistered = true;

    registerOperatorActions(api, config, () => readiness.store, createReadinessHandler(readiness));

    api.on("after_tool_call", handleAfterSpawnTool, { priority: 100, timeoutMs: 2_000 });

    api.on("subagent_ended", async (event: any) => {
      if (!readiness.store || !event.runId) return;
      try {
        const row = await readiness.store.request("getByRun", { runId: event.runId });
        if (!row || row.child_session_key !== event.targetSessionKey || row.state !== "bound")
          return;
        const task = resolveTaskForReservation(api, row);
        if (task && isTerminalTask(task)) await readiness.store.release(row);
      } catch (error) {
        api.logger.error(`workspace-lane terminal reconciliation deferred: ${boundedCode(error)}`);
      }
    });

    async function startGeneration(): Promise<void> {
      shared.owners.add(instanceId);
      if (
        readiness.store &&
        readiness.workerReady &&
        readiness.gatewayGenerationId &&
        readiness.store.generationId === readiness.gatewayGenerationId &&
        reconciler.ready &&
        reconciler.generationId === readiness.gatewayGenerationId
      )
        return;
      if (shared.startPromise) return shared.startPromise;
      shared.startPromise = (async () => {
        const generationId = crypto.randomUUID();
        readiness.gatewayGenerationId = generationId;
        readiness.gatewayStartedAt = Date.now();
        readiness.workerReady = false;
        reconciler.ready = false;
        admission.generationId = generationId;
        admission.workerReady = false;
        admission.reconcilerReady = false;
        if (shared.reconcileTimer) clearInterval(shared.reconcileTimer);
        shared.reconcileTimer = null;
        const previousStore = readiness.store;
        readiness.store = null;
        admission.store = null;
        if (previousStore) {
          try {
            await previousStore.terminate();
          } catch {}
        }
        let store: ReservationStore | null = null;
        try {
          store = new ReservationStore(config.stateDir, generationId, config.acquisitionDeadlineMs);
          readiness.store = store;
          admission.store = store;
          await store.initialize();
          readiness.workerReady = true;
          admission.workerReady = true;
          await runReconciliation();
          shared.reconcileTimer = setInterval(runReconciliation, 10_000);
          shared.reconcileTimer.unref();
        } catch (error) {
          readiness.workerReady = false;
          reconciler.ready = false;
          admission.workerReady = false;
          admission.reconcilerReady = false;
          if (readiness.store === store) readiness.store = null;
          if (admission.store === store) admission.store = null;
          if (store) {
            try {
              await store.terminate();
            } catch {}
          }
          api.logger.error(`workspace-lane startup failed closed: ${boundedCode(error)}`);
        }
      })().finally(() => {
        shared.startPromise = null;
      });
      return shared.startPromise;
    }

    function beginGeneration(): void {
      shared.owners.add(instanceId);
      void startGeneration();
    }

    async function stopGeneration(): Promise<void> {
      shared.owners.delete(instanceId);
      if (shared.owners.size > 0) return;
      if (shared.reconcileTimer) clearInterval(shared.reconcileTimer);
      shared.reconcileTimer = null;
      readiness.workerReady = false;
      reconciler.ready = false;
      admission.workerReady = false;
      admission.reconcilerReady = false;
      if (readiness.store) {
        try {
          await readiness.store.terminate();
        } catch {}
      }
      readiness.store = null;
      admission.store = null;
    }

    // gateway_start is the native generation signal. The service start uses
    // the same idempotent initializer so an active-registry replacement during
    // startup/reload cannot leave the action registry attached to an unstarted
    // plugin instance.
    api.on("gateway_start", beginGeneration, { priority: 100, timeoutMs: 5_000 });
    api.on("gateway_stop", stopGeneration, { priority: 100, timeoutMs: 2_000 });
    api.registerService({
      id: "workspace-lane-runtime",
      start: beginGeneration,
      stop: stopGeneration,
    });

    api.lifecycle.registerRuntimeLifecycle({
      id: "workspace-lane-worker",
      description: "Terminate the reservation worker on plugin reset, delete, disable, or restart",
      cleanup: stopGeneration,
    });
  },
});
