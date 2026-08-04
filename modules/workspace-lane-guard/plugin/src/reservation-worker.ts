import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { parentPort, workerData } from "node:worker_threads";

if (!parentPort) throw new Error("RESERVATION_WORKER_REQUIRES_PARENT");
const port = parentPort;

const databasePath = String(workerData.databasePath);
const generationId = String(workerData.generationId);
const initializationDeadlineMs = Number(workerData.initializationDeadlineMs);
if (!Number.isFinite(initializationDeadlineMs) || initializationDeadlineMs < 1) {
  throw new Error("INVALID_RESERVATION_INITIALIZATION_DEADLINE");
}
fs.mkdirSync(path.dirname(databasePath), { recursive: true, mode: 0o700 });
fs.chmodSync(path.dirname(databasePath), 0o700);

const db = new DatabaseSync(databasePath);

function isRetryableBusy(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return /SQLITE_BUSY|database is locked/i.test(message);
}

function initializationJitter(attempt: number, remainingMs: number): Promise<void> {
  const base = Math.min(40, 4 * 2 ** attempt);
  const jitter = crypto.randomInt(0, Math.max(1, Math.floor(base / 2)));
  const delayMs = Math.min(remainingMs, base + jitter);
  return new Promise((resolve) => setTimeout(resolve, delayMs));
}

async function initializeDatabase(): Promise<void> {
  const startedAt = Date.now();
  let attempt = 0;

  for (;;) {
    const remainingMs = initializationDeadlineMs - (Date.now() - startedAt);
    if (remainingMs <= 0) {
      throw new Error("RESERVATION_DATABASE_INITIALIZATION_BUSY_DEADLINE");
    }

    // Limit SQLite's own lock wait to the remaining initialization budget.
    // Additional jittered retries happen only in this dedicated worker.
    db.exec(`PRAGMA busy_timeout = ${Math.max(1, Math.min(50, remainingMs))};`);
    try {
      db.exec(`
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous = FULL;
        PRAGMA foreign_keys = ON;
        PRAGMA trusted_schema = OFF;
        PRAGMA wal_autocheckpoint = 1000;
        CREATE TABLE IF NOT EXISTS metadata (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        ) STRICT;
        CREATE TABLE IF NOT EXISTS reservations (
          authority_key TEXT PRIMARY KEY,
          authority_root TEXT NOT NULL,
          authority_components_json TEXT NOT NULL,
          access TEXT NOT NULL CHECK(access IN ('ro', 'rw')),
          owner_session_key TEXT NOT NULL,
          owner_agent_id TEXT NOT NULL,
          target_agent_id TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('provisional', 'bound', 'quarantined')),
          claim_token TEXT NOT NULL UNIQUE,
          conflict_id TEXT NOT NULL,
          native_task_id TEXT,
          run_id TEXT,
          child_session_key TEXT,
          claimed_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          quarantine_code TEXT
        ) STRICT;
        CREATE TABLE IF NOT EXISTS barriers (
          authority_key TEXT PRIMARY KEY,
          authority_root TEXT NOT NULL,
          authority_components_json TEXT NOT NULL,
          owner_session_key TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('lane_closing', 'operator_quarantine')),
          barrier_token TEXT NOT NULL UNIQUE,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          reason_code TEXT NOT NULL
        ) STRICT;
        CREATE INDEX IF NOT EXISTS reservations_owner_idx ON reservations(owner_session_key);
        CREATE INDEX IF NOT EXISTS reservations_run_idx ON reservations(run_id);
        CREATE INDEX IF NOT EXISTS barriers_owner_idx ON barriers(owner_session_key);
      `);
      db.prepare(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES ('schema_version', '1')",
      ).run();
      db.prepare("INSERT OR REPLACE INTO metadata(key, value) VALUES ('worker_generation', ?)").run(
        generationId,
      );
      db.exec("PRAGMA busy_timeout = 50;");
      return;
    } catch (error) {
      if (!isRetryableBusy(error)) throw error;
      attempt += 1;
      const retryBudgetMs = initializationDeadlineMs - (Date.now() - startedAt);
      if (retryBudgetMs <= 0) {
        throw new Error("RESERVATION_DATABASE_INITIALIZATION_BUSY_DEADLINE");
      }
      await initializationJitter(attempt, retryBudgetMs);
    }
  }
}

await initializeDatabase();

function protectFiles() {
  for (const suffix of ["", "-wal", "-shm"]) {
    const candidate = `${databasePath}${suffix}`;
    if (fs.existsSync(candidate)) fs.chmodSync(candidate, 0o600);
  }
}
protectFiles();

function components(raw: unknown): string[] {
  const parsed = JSON.parse(String(raw));
  if (!Array.isArray(parsed) || parsed.some((item) => typeof item !== "string")) {
    throw new Error("INVALID_AUTHORITY_COMPONENTS");
  }
  return parsed;
}

function relation(
  left: string[],
  right: string[],
): "equal" | "ancestor" | "descendant" | "disjoint" {
  const common = Math.min(left.length, right.length);
  for (let index = 0; index < common; index += 1)
    if (left[index] !== right[index]) return "disjoint";
  if (left.length === right.length) return "equal";
  return left.length < right.length ? "ancestor" : "descendant";
}

function findConflict(candidate: string[], requesterSessionKey: string) {
  const reservationRows = db
    .prepare(
      "SELECT authority_root, authority_components_json, conflict_id, native_task_id, owner_session_key FROM reservations",
    )
    .all();
  for (const row of reservationRows) {
    const found = relation(candidate, components(row.authority_components_json));
    if (found !== "disjoint") {
      const sameControllingSession = row.owner_session_key === requesterSessionKey;
      return {
        code: found === "equal" ? "AUTHORITY_ROOT_RESERVED" : "AUTHORITY_TREE_RESERVED",
        authorityRoot: row.authority_root,
        conflictId: row.conflict_id,
        holderTaskId: sameControllingSession ? (row.native_task_id ?? null) : null,
        sameControllingSession,
      };
    }
  }
  const barrierRows = db
    .prepare("SELECT authority_root, authority_components_json, barrier_token FROM barriers")
    .all();
  for (const row of barrierRows) {
    const found = relation(candidate, components(row.authority_components_json));
    if (found !== "disjoint") {
      return {
        code: found === "equal" ? "AUTHORITY_ROOT_RESERVED" : "AUTHORITY_TREE_RESERVED",
        authorityRoot: row.authority_root,
        conflictId: row.barrier_token,
        holderTaskId: null,
        sameControllingSession: false,
      };
    }
  }
  return null;
}

function acquire(input: Record<string, any>) {
  db.exec("BEGIN IMMEDIATE");
  try {
    const candidate = components(input.authorityComponentsJson);
    const conflict = findConflict(candidate, input.ownerSessionKey);
    if (conflict) {
      db.exec("ROLLBACK");
      return { acquired: false, conflict };
    }
    db.prepare(`
      INSERT INTO reservations (
        authority_key, authority_root, authority_components_json, access,
        owner_session_key, owner_agent_id, target_agent_id, state,
        claim_token, conflict_id, claimed_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, 'provisional', ?, ?, ?, ?)
    `).run(
      input.authorityKey,
      input.authorityRoot,
      input.authorityComponentsJson,
      input.access,
      input.ownerSessionKey,
      input.ownerAgentId,
      input.targetAgentId,
      input.claimToken,
      input.conflictId,
      input.now,
      input.now,
    );
    db.exec("COMMIT");
    protectFiles();
    return { acquired: true, claimToken: input.claimToken, conflictId: input.conflictId };
  } catch (error) {
    try {
      db.exec("ROLLBACK");
    } catch {}
    throw error;
  }
}

function bind(input: Record<string, any>) {
  const result = db
    .prepare(`
    UPDATE reservations
    SET state = 'bound', native_task_id = ?, run_id = ?, child_session_key = ?,
        updated_at = ?, quarantine_code = NULL
    WHERE authority_key = ? AND claim_token = ? AND state = 'provisional'
  `)
    .run(
      input.nativeTaskId,
      input.runId,
      input.childSessionKey,
      input.now,
      input.authorityKey,
      input.claimToken,
    );
  return { changed: Number(result.changes) };
}

function release(input: Record<string, any>) {
  const result = db
    .prepare("DELETE FROM reservations WHERE authority_key = ? AND claim_token = ?")
    .run(input.authorityKey, input.claimToken);
  return { changed: Number(result.changes) };
}

function quarantine(input: Record<string, any>) {
  const result = db
    .prepare(`
    UPDATE reservations SET state = 'quarantined', quarantine_code = ?, updated_at = ?
    WHERE authority_key = ? AND claim_token = ?
  `)
    .run(input.code, input.now, input.authorityKey, input.claimToken);
  return { changed: Number(result.changes) };
}

function beginBarrier(input: Record<string, any>) {
  db.exec("BEGIN IMMEDIATE");
  try {
    const candidate = components(input.authorityComponentsJson);
    const conflict = findConflict(candidate, input.ownerSessionKey);
    if (conflict) {
      db.exec("ROLLBACK");
      return { created: false, conflict };
    }
    db.prepare(`
      INSERT INTO barriers (
        authority_key, authority_root, authority_components_json,
        owner_session_key, state, barrier_token, created_at, updated_at, reason_code
      ) VALUES (?, ?, ?, ?, 'lane_closing', ?, ?, ?, ?)
    `).run(
      input.authorityKey,
      input.authorityRoot,
      input.authorityComponentsJson,
      input.ownerSessionKey,
      input.barrierToken,
      input.now,
      input.now,
      input.reasonCode,
    );
    db.exec("COMMIT");
    return { created: true, barrierToken: input.barrierToken };
  } catch (error) {
    try {
      db.exec("ROLLBACK");
    } catch {}
    throw error;
  }
}

function completeBarrier(input: Record<string, any>) {
  const result = db
    .prepare(
      "DELETE FROM barriers WHERE authority_key = ? AND barrier_token = ? AND owner_session_key = ?",
    )
    .run(input.authorityKey, input.barrierToken, input.ownerSessionKey);
  return { changed: Number(result.changes) };
}

function forceClear(input: Record<string, any>) {
  db.exec("BEGIN IMMEDIATE");
  try {
    const reservation = db
      .prepare("SELECT * FROM reservations WHERE authority_key = ?")
      .get(input.authorityKey);
    if (!reservation) {
      db.exec("ROLLBACK");
      return { changed: 0, reservation: null };
    }
    const result = db
      .prepare("DELETE FROM reservations WHERE authority_key = ? AND claim_token = ?")
      .run(input.authorityKey, input.claimToken);
    db.exec("COMMIT");
    return { changed: Number(result.changes), reservation };
  } catch (error) {
    try {
      db.exec("ROLLBACK");
    } catch {}
    throw error;
  }
}

function handle(operation: string, input: Record<string, any>) {
  switch (operation) {
    case "ping":
      return { ready: true, generationId, now: Date.now() };
    case "quickCheck": {
      const row = db.prepare("PRAGMA quick_check").get() as Record<string, unknown> | undefined;
      return { value: row?.quick_check ?? "missing" };
    }
    case "integrityCheck":
      return { rows: db.prepare("PRAGMA integrity_check").all() };
    case "acquire":
      return acquire(input);
    case "bind":
      return bind(input);
    case "release":
      return release(input);
    case "quarantine":
      return quarantine(input);
    case "beginBarrier":
      return beginBarrier(input);
    case "completeBarrier":
      return completeBarrier(input);
    case "forceClear":
      return forceClear(input);
    case "list":
      return {
        reservations: db.prepare("SELECT * FROM reservations ORDER BY updated_at DESC").all(),
        barriers: db.prepare("SELECT * FROM barriers ORDER BY updated_at DESC").all(),
      };
    case "getByRun":
      return db.prepare("SELECT * FROM reservations WHERE run_id = ?").get(input.runId) ?? null;
    case "getByOwner":
      return db
        .prepare("SELECT * FROM reservations WHERE owner_session_key = ? ORDER BY updated_at DESC")
        .all(input.ownerSessionKey);
    case "getBarrier":
      return (
        db
          .prepare(
            "SELECT * FROM barriers WHERE owner_session_key = ? ORDER BY updated_at DESC LIMIT 1",
          )
          .get(input.ownerSessionKey) ?? null
      );
    case "checkpoint":
      return db.prepare("PRAGMA wal_checkpoint(FULL)").all();
    default:
      throw new Error(`UNKNOWN_WORKER_OPERATION:${operation}`);
  }
}

port.on("message", (message) => {
  const id = message?.id;
  try {
    const result = handle(message.operation, message.input ?? {});
    protectFiles();
    port.postMessage({ id, ok: true, result });
  } catch (error) {
    const code = error instanceof Error ? error.message : String(error);
    port.postMessage({ id, ok: false, error: code.slice(0, 240) });
  }
});

port.on("close", () => {
  try {
    db.close();
  } catch {}
});
