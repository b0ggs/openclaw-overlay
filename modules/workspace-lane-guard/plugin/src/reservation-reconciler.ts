import type { ReservationStore } from "./reservation-db.ts";
import { isActiveTask, isTerminalTask, resolveTaskForReservation } from "./task-adapter.ts";

export type ReconcilerState = {
  ready: boolean;
  generationId: string | null;
  lastReconciledAt: number;
  lastErrorCode: string | null;
};

export function createReconcilerState(): ReconcilerState {
  return { ready: false, generationId: null, lastReconciledAt: 0, lastErrorCode: null };
}

async function quarantineRow(
  store: ReservationStore,
  row: Record<string, unknown>,
  code: string,
): Promise<void> {
  if (row.state === "quarantined") return;
  await store.quarantine(row, code);
}

export async function reconcileReservations(
  api: any,
  store: ReservationStore,
  generationId: string,
  state: ReconcilerState,
): Promise<void> {
  state.ready = false;
  state.generationId = generationId;
  try {
    const snapshot = await store.list();
    for (const row of snapshot.reservations) {
      if (row.state === "provisional") {
        await quarantineRow(store, row, "RESTART_PROVISIONAL_AMBIGUITY");
        continue;
      }
      if (row.state === "quarantined") continue;
      let task;
      try {
        task = resolveTaskForReservation(api, row);
      } catch {
        await quarantineRow(store, row, "NATIVE_TASK_AMBIGUOUS");
        continue;
      }
      if (!task) {
        await quarantineRow(store, row, "NATIVE_TASK_MISSING");
      } else if (isTerminalTask(task)) {
        await store.release(row);
      } else if (!isActiveTask(task)) {
        await quarantineRow(store, row, "NATIVE_TASK_STATE_UNKNOWN");
      }
    }
    state.lastReconciledAt = Date.now();
    state.lastErrorCode = null;
    state.ready = true;
  } catch (error) {
    state.lastErrorCode =
      error instanceof Error ? error.message.slice(0, 160) : "RECONCILIATION_FAILED";
    throw error;
  }
}
