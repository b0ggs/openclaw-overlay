import type { ReservationStore } from "./reservation-db.ts";
import type { ReconcilerState } from "./reservation-reconciler.ts";
import { PLUGIN_ID, PLUGIN_VERSION, POLICY_ID } from "./lane-schema.ts";

export type RuntimeReadiness = {
  gatewayGenerationId: string | null;
  gatewayStartedAt: number;
  policyRegistered: boolean;
  workerReady: boolean;
  reconciler: ReconcilerState;
  store: ReservationStore | null;
};

export function createReadinessHandler(state: RuntimeReadiness) {
  const observedChallenges = new Set<string>();
  return async (ctx: any) => {
    const challenge = (ctx.payload as Record<string, unknown> | undefined)?.challenge;
    if (typeof challenge !== "string" || !/^[A-Za-z0-9_-]{16,128}$/.test(challenge)) {
      return { ok: false, code: "INVALID_CHALLENGE", error: "INVALID_CHALLENGE" };
    }
    if (observedChallenges.has(challenge)) {
      return { ok: false, code: "REPLAYED_CHALLENGE", error: "REPLAYED_CHALLENGE" };
    }
    observedChallenges.add(challenge);
    if (observedChallenges.size > 1024)
      observedChallenges.delete(observedChallenges.values().next().value!);
    let workerReady = false;
    if (state.store && state.gatewayGenerationId) {
      try {
        const ping = await state.store.request("ping", {}, 400);
        workerReady = ping?.ready === true && ping.generationId === state.gatewayGenerationId;
      } catch {
        workerReady = false;
      }
    }
    const generationCoherent =
      Boolean(state.gatewayGenerationId) &&
      state.gatewayStartedAt > 0 &&
      state.reconciler.generationId === state.gatewayGenerationId &&
      state.reconciler.lastReconciledAt >= state.gatewayStartedAt;
    const ready =
      generationCoherent &&
      state.policyRegistered &&
      state.workerReady &&
      workerReady &&
      state.reconciler.ready;
    return {
      ok: true,
      result: {
        schemaVersion: 1,
        ready,
        ...(!ready ? { reasonCode: "RUNTIME_COMPONENT_UNREADY" } : {}),
        pluginId: PLUGIN_ID,
        pluginVersion: PLUGIN_VERSION,
        gatewayGenerationId: state.gatewayGenerationId ?? "",
        gatewayStartedAt: state.gatewayStartedAt,
        policyId: POLICY_ID,
        policyRegistered: state.policyRegistered,
        workerReady,
        reconcilerReady: state.reconciler.ready && generationCoherent,
        lastReconciledAt: state.reconciler.lastReconciledAt,
        challenge,
        respondedAt: Date.now(),
      },
    };
  };
}
