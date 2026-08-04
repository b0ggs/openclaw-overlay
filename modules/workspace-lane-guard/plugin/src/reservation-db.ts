import crypto from "node:crypto";
import path from "node:path";
import { Worker } from "node:worker_threads";
import type { LaneRecord } from "./lane-schema.ts";
import { componentsFor } from "./path-policy.ts";

type Pending = {
  resolve: (value: any) => void;
  reject: (error: Error) => void;
  timer: NodeJS.Timeout;
};

// Gateway startup loads and pre-warms many plugins concurrently. The worker's
// module/bootstrap reply is not part of the public reservation-acquisition
// budget, so give that transport enough time on a cold, resource-constrained
// Gateway while governed admission remains closed. Database initialization
// and every reservation transaction still retain the configured deadline.
const WORKER_BOOTSTRAP_TRANSPORT_DEADLINE_MS = 15_000;

function isRetryableBusy(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return /SQLITE_BUSY|database is locked/i.test(message);
}

function boundedJitter(attempt: number): Promise<void> {
  const base = Math.min(40, 4 * 2 ** attempt);
  const jitter = crypto.randomInt(0, Math.max(1, Math.floor(base / 2)));
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, base + jitter);
    timer.unref();
  });
}

export type ReservationClaim = {
  authorityKey: string;
  authorityRoot: string;
  claimToken: string;
  conflictId: string;
};

export class ReservationStore {
  readonly databasePath: string;
  readonly generationId: string;
  readonly deadlineMs: number;
  #worker: Worker;
  #nextId = 1;
  #pending = new Map<number, Pending>();
  #deadError: Error | null = null;

  constructor(stateDir: string, generationId: string, deadlineMs = 500) {
    this.databasePath = path.join(
      stateDir,
      "plugins",
      "workspace-lane-guard",
      "reservations.sqlite",
    );
    this.generationId = generationId;
    this.deadlineMs = deadlineMs;
    this.#worker = new Worker(new URL("./reservation-worker.ts", import.meta.url), {
      workerData: {
        databasePath: this.databasePath,
        generationId,
        // Leave a small transport margin inside the same public deadline so
        // the worker can return its stable fail-closed initialization code.
        initializationDeadlineMs:
          deadlineMs - Math.min(25, Math.max(5, Math.floor(deadlineMs / 10))),
      },
      // OpenClaw may disable native stripping in the Gateway process because it
      // owns plugin loading. The isolated built-in-only worker enables it
      // explicitly for this reviewed source file.
      execArgv: ["--experimental-strip-types"],
    });
    this.#worker.on("message", (message) => {
      const pending = this.#pending.get(message.id);
      if (!pending) return;
      clearTimeout(pending.timer);
      this.#pending.delete(message.id);
      if (message.ok) pending.resolve(message.result);
      else pending.reject(new Error(message.error || "RESERVATION_WORKER_ERROR"));
    });
    this.#worker.on("error", (error) =>
      this.#failAll(error instanceof Error ? error : new Error(String(error))),
    );
    this.#worker.on("exit", (code) => {
      if (code !== 0) this.#failAll(new Error(`RESERVATION_WORKER_EXIT:${code}`));
    });
  }

  #failAll(error: Error): void {
    if (this.#deadError) return;
    this.#deadError = error;
    for (const pending of this.#pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.#pending.clear();
  }

  request(
    operation: string,
    input: Record<string, unknown> = {},
    deadlineMs = this.deadlineMs,
  ): Promise<any> {
    if (this.#deadError) return Promise.reject(this.#deadError);
    const id = this.#nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.#pending.delete(id);
        reject(new Error("RESERVATION_WORKER_DEADLINE"));
      }, deadlineMs);
      timer.unref();
      this.#pending.set(id, { resolve, reject, timer });
      this.#worker.postMessage({ id, operation, input });
    });
  }

  async #transactionRequest(operation: string, input: Record<string, unknown>): Promise<any> {
    const startedAt = Date.now();
    let attempt = 0;
    for (;;) {
      const remaining = this.deadlineMs - (Date.now() - startedAt);
      if (remaining <= 0) throw new Error("RESERVATION_BUSY_DEADLINE");
      try {
        return await this.request(operation, input, remaining);
      } catch (error) {
        if (!isRetryableBusy(error)) throw error;
        attempt += 1;
        if (attempt >= 8) throw new Error("RESERVATION_BUSY_DEADLINE");
        await boundedJitter(attempt);
      }
    }
  }

  async initialize(): Promise<void> {
    const startedAt = Date.now();
    // Worker module loading is outside the reservation acquisition budget and
    // may exceed 500 ms in a cold Gateway. Keep it bounded independently;
    // governed spawns remain closed until readiness and every database
    // initialization/acquisition attempt retains the configured deadline.
    const ping = await this.request("ping", {}, WORKER_BOOTSTRAP_TRANSPORT_DEADLINE_MS);
    if (!ping?.ready || ping.generationId !== this.generationId)
      throw new Error("RESERVATION_WORKER_WRONG_GENERATION");
    const remaining = WORKER_BOOTSTRAP_TRANSPORT_DEADLINE_MS - (Date.now() - startedAt);
    if (remaining <= 0) throw new Error("RESERVATION_WORKER_DEADLINE");
    const quick = await this.request("quickCheck", {}, remaining);
    if (quick?.value !== "ok") throw new Error("RESERVATION_DATABASE_QUICK_CHECK_FAILED");
  }

  async acquire(
    lane: LaneRecord,
  ): Promise<ReservationClaim | { conflict: Record<string, unknown> }> {
    const claimToken = crypto.randomUUID();
    const conflictId = crypto.randomUUID();
    const authorityKey = `sha256:${crypto.createHash("sha256").update(lane.authorityRoot).digest("hex")}`;
    const result = await this.#transactionRequest("acquire", {
      authorityKey,
      authorityRoot: lane.authorityRoot,
      authorityComponentsJson: JSON.stringify(componentsFor(lane.authorityRoot)),
      access: lane.access,
      ownerSessionKey: lane.parentSessionKey,
      ownerAgentId: lane.parentAgentId,
      targetAgentId: lane.targetAgentId,
      claimToken,
      conflictId,
      now: Date.now(),
    });
    if (!result.acquired) return { conflict: result.conflict };
    return { authorityKey, authorityRoot: lane.authorityRoot, claimToken, conflictId };
  }

  async bind(
    claim: ReservationClaim,
    task: { id: string; runId: string; childSessionKey: string },
  ): Promise<void> {
    const result = await this.request("bind", {
      authorityKey: claim.authorityKey,
      claimToken: claim.claimToken,
      nativeTaskId: task.id,
      runId: task.runId,
      childSessionKey: task.childSessionKey,
      now: Date.now(),
    });
    if (result.changed !== 1) throw new Error("RESERVATION_LOST_OWNERSHIP");
  }

  async release(row: Record<string, unknown>): Promise<void> {
    const result = await this.request("release", {
      authorityKey: row.authority_key,
      claimToken: row.claim_token,
    });
    if (result.changed !== 1) throw new Error("RESERVATION_LOST_OWNERSHIP");
  }

  async quarantine(claim: ReservationClaim | Record<string, unknown>, code: string): Promise<void> {
    const authorityKey = "authorityKey" in claim ? claim.authorityKey : claim.authority_key;
    const claimToken = "claimToken" in claim ? claim.claimToken : claim.claim_token;
    const result = await this.request("quarantine", {
      authorityKey,
      claimToken,
      code,
      now: Date.now(),
    });
    if (result.changed !== 1) throw new Error("RESERVATION_LOST_OWNERSHIP");
  }

  async beginClear(lane: LaneRecord, ownerSessionKey: string): Promise<Record<string, unknown>> {
    const barrierToken = crypto.randomUUID();
    const authorityKey = `sha256:${crypto.createHash("sha256").update(lane.authorityRoot).digest("hex")}`;
    return this.#transactionRequest("beginBarrier", {
      authorityKey,
      authorityRoot: lane.authorityRoot,
      authorityComponentsJson: JSON.stringify(componentsFor(lane.authorityRoot)),
      ownerSessionKey,
      barrierToken,
      reasonCode: "EXPLICIT_LANE_CLEAR",
      now: Date.now(),
    });
  }

  async completeClear(
    ownerSessionKey: string,
    authorityKey: string,
    barrierToken: string,
  ): Promise<void> {
    const result = await this.request("completeBarrier", {
      ownerSessionKey,
      authorityKey,
      barrierToken,
    });
    if (result.changed !== 1) throw new Error("CLEAR_BARRIER_LOST");
  }

  async list(): Promise<{
    reservations: Record<string, unknown>[];
    barriers: Record<string, unknown>[];
  }> {
    return this.request("list");
  }

  async terminate(): Promise<void> {
    await this.#worker.terminate();
  }
}
