import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { ReservationStore } from "../../plugin/src/reservation-db.ts";
import {
  createReconcilerState,
  reconcileReservations,
} from "../../plugin/src/reservation-reconciler.ts";
import { resolveMatchingNativeTask } from "../../plugin/src/task-adapter.ts";

function lane(root, session = "agent:main:main") {
  return {
    schemaVersion: 3,
    parentSessionKey: session,
    parentAgentId: "main",
    targetAgentId: "worker",
    workspaceRoot: root,
    authorityRoot: root,
    taskRoot: root,
    access: "rw",
    issuedAt: Date.now(),
    expiresAt: Date.now() + 60_000,
    issuerAuthority: "operator.admin",
    issuanceId: crypto.randomUUID(),
    configFingerprint: "c",
    workspaceFingerprint: "w",
    openclawVersion: "v",
    pluginVersion: "0.1.0",
  };
}

async function fixture(run) {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), "wlg-db-"));
  const roots = ["a", "b", "a/nested"].map((name) => path.join(base, name));
  for (const root of roots) fs.mkdirSync(root, { recursive: true });
  const store = new ReservationStore(base, "generation", 500);
  await store.initialize();
  try {
    await run({ base, roots, store });
  } finally {
    await store.terminate();
    fs.rmSync(base, { recursive: true, force: true });
  }
}

test("exact acquisition conflicts and claim-token release is conditional", () =>
  fixture(async ({ roots, store }) => {
    const first = await store.acquire(lane(roots[0]));
    assert.equal("conflict" in first, false);
    const second = await store.acquire(lane(roots[0], "agent:main:other"));
    assert.equal(second.conflict.code, "AUTHORITY_ROOT_RESERVED");
    await assert.rejects(
      store.release({ authority_key: first.authorityKey, claim_token: "stale" }),
      /LOST_OWNERSHIP/,
    );
    await store.release({ authority_key: first.authorityKey, claim_token: first.claimToken });
  }));

test("ancestor and descendant reservations conflict while disjoint roots proceed", () =>
  fixture(async ({ roots, store }) => {
    const nested = await store.acquire(lane(roots[2]));
    const ancestor = await store.acquire(lane(roots[0]));
    assert.equal(ancestor.conflict.code, "AUTHORITY_TREE_RESERVED");
    const disjoint = await store.acquire(lane(roots[1]));
    assert.equal("conflict" in disjoint, false);
    await store.release({ authority_key: nested.authorityKey, claim_token: nested.claimToken });
    await store.release({ authority_key: disjoint.authorityKey, claim_token: disjoint.claimToken });
  }));

test("native task metadata binds and terminal reconciliation releases the reservation", () =>
  fixture(async ({ roots, store }) => {
    const record = lane(roots[0]);
    const claim = await store.acquire(record);
    const nativeTask = {
      id: "task-1",
      runtime: "subagent",
      sessionKey: record.parentSessionKey,
      childSessionKey: "agent:worker:subagent:child-1",
      agentId: record.targetAgentId,
      runId: "run-1",
      status: "running",
      createdAt: Date.now(),
    };
    const api = {
      runtime: {
        tasks: {
          runs: {
            bindSession: ({ sessionKey }) => ({
              list: () => (sessionKey === record.parentSessionKey ? [nativeTask] : []),
            }),
          },
        },
      },
    };
    const matched = resolveMatchingNativeTask(
      api,
      record.parentSessionKey,
      { runId: nativeTask.runId, childSessionKey: nativeTask.childSessionKey },
      { targetAgentId: record.targetAgentId, claimedAt: nativeTask.createdAt },
    );
    await store.bind(claim, matched);
    let snapshot = await store.list();
    assert.equal(snapshot.reservations.length, 1);
    assert.equal(snapshot.reservations[0].state, "bound");
    assert.equal(snapshot.reservations[0].native_task_id, nativeTask.id);
    assert.equal(snapshot.reservations[0].run_id, nativeTask.runId);
    assert.equal(snapshot.reservations[0].child_session_key, nativeTask.childSessionKey);

    nativeTask.status = "succeeded";
    const reconciler = createReconcilerState();
    await reconcileReservations(api, store, "generation", reconciler);
    snapshot = await store.list();
    assert.equal(snapshot.reservations.length, 0);
    assert.equal(reconciler.ready, true);
  }));

test("barriers use the same hierarchical conflict scan", () =>
  fixture(async ({ roots, store }) => {
    const record = lane(roots[0]);
    const barrier = await store.beginClear(record, record.parentSessionKey);
    assert.equal(barrier.created, true);
    const nested = await store.acquire(lane(roots[2]));
    assert.equal(nested.conflict.code, "AUTHORITY_TREE_RESERVED");
    const secondBarrier = await store.beginClear(lane(roots[2]), "other");
    assert.equal(secondBarrier.created, false);
    await store.completeClear(
      record.parentSessionKey,
      `sha256:${crypto.createHash("sha256").update(record.authorityRoot).digest("hex")}`,
      barrier.barrierToken,
    );
  }));

test("bind, quarantine, status, and integrity survive restart", () =>
  fixture(async ({ base, roots, store }) => {
    const claim = await store.acquire(lane(roots[0]));
    await store.bind(claim, { id: "task", runId: "run", childSessionKey: "child" });
    let snapshot = await store.list();
    assert.equal(snapshot.reservations[0].state, "bound");
    await store.quarantine(snapshot.reservations[0], "AMBIGUOUS");
    snapshot = await store.list();
    assert.equal(snapshot.reservations[0].quarantine_code, "AMBIGUOUS");
    assert.equal((await store.request("quickCheck")).value, "ok");
    await store.terminate();
    const restarted = new ReservationStore(base, "generation-2", 500);
    await restarted.initialize();
    try {
      assert.equal((await restarted.list()).reservations[0].state, "quarantined");
      assert.equal((await restarted.request("integrityCheck")).rows[0].integrity_check, "ok");
    } finally {
      await restarted.terminate();
    }
  }));

test("restart reconciliation preserves active work, releases terminal work, and quarantines uncertain records", async () => {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), "wlg-restart-"));
  const names = ["active", "terminal", "missing", "ambiguous", "provisional"];
  const roots = Object.fromEntries(
    names.map((name) => {
      const root = path.join(base, name);
      fs.mkdirSync(root, { recursive: true });
      return [name, root];
    }),
  );
  const original = new ReservationStore(base, "generation-1", 500);
  let restarted;
  await original.initialize();
  try {
    const records = Object.fromEntries(
      names.map((name) => [name, lane(roots[name], `agent:main:${name}`)]),
    );
    const claims = {};
    for (const name of names) claims[name] = await original.acquire(records[name]);
    for (const name of ["active", "terminal", "missing", "ambiguous"]) {
      await original.bind(claims[name], {
        id: `task-${name}`,
        runId: `run-${name}`,
        childSessionKey: `agent:worker:subagent:${name}`,
      });
    }

    await original.terminate();
    restarted = new ReservationStore(base, "generation-2", 500);
    await restarted.initialize();

    const taskFor = (name, status = "running") => ({
      id: `task-${name}`,
      runtime: "subagent",
      sessionKey: records[name].parentSessionKey,
      childSessionKey: `agent:worker:subagent:${name}`,
      agentId: records[name].targetAgentId,
      runId: `run-${name}`,
      status,
      createdAt: Date.now(),
    });
    const api = {
      runtime: {
        tasks: {
          runs: {
            bindSession: ({ sessionKey }) => ({
              list: () => {
                if (sessionKey === records.active.parentSessionKey) return [taskFor("active")];
                if (sessionKey === records.terminal.parentSessionKey)
                  return [taskFor("terminal", "succeeded")];
                if (sessionKey === records.missing.parentSessionKey)
                  return [{ ...taskFor("missing"), runId: "mismatched-run" }];
                if (sessionKey === records.ambiguous.parentSessionKey)
                  return [taskFor("ambiguous"), taskFor("ambiguous")];
                return [];
              },
            }),
          },
        },
      },
    };
    const reconciler = createReconcilerState();
    const recovery = reconcileReservations(api, restarted, "generation-2", reconciler);
    assert.equal(reconciler.ready, false);
    await recovery;

    const snapshot = await restarted.list();
    const byRoot = new Map(snapshot.reservations.map((row) => [row.authority_root, row]));
    assert.equal(byRoot.get(roots.active).state, "bound");
    assert.equal(byRoot.has(roots.terminal), false);
    assert.equal(byRoot.get(roots.missing).state, "quarantined");
    assert.equal(byRoot.get(roots.missing).quarantine_code, "NATIVE_TASK_MISSING");
    assert.equal(byRoot.get(roots.ambiguous).state, "quarantined");
    assert.equal(byRoot.get(roots.ambiguous).quarantine_code, "NATIVE_TASK_AMBIGUOUS");
    assert.equal(byRoot.get(roots.provisional).state, "quarantined");
    assert.equal(byRoot.get(roots.provisional).quarantine_code, "RESTART_PROVISIONAL_AMBIGUITY");
    assert.equal(reconciler.ready, true);
    assert.equal(reconciler.generationId, "generation-2");
    assert.ok(reconciler.lastReconciledAt > 0);
    assert.equal(reconciler.lastErrorCode, null);
    assert.equal((await restarted.request("integrityCheck")).rows[0].integrity_check, "ok");
  } finally {
    await Promise.allSettled([original.terminate(), restarted?.terminate()]);
    fs.rmSync(base, { recursive: true, force: true });
  }
});

test("WAL state is protected and checkpoint is supported", () =>
  fixture(async ({ roots, store }) => {
    const claim = await store.acquire(lane(roots[0]));
    const db = store.databasePath;
    assert.equal(fs.statSync(db).mode & 0o777, 0o600);
    if (fs.existsSync(`${db}-wal`)) assert.equal(fs.statSync(`${db}-wal`).mode & 0o777, 0o600);
    await store.request("checkpoint");
    await store.release({ authority_key: claim.authorityKey, claim_token: claim.claimToken });
    assert.equal(fs.statSync(path.dirname(db)).mode & 0o077, 0);
  }));

test("two worker actors produce one winner in 100 exact-root races", async () => {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), "wlg-race-"));
  const a = new ReservationStore(base, "a", 500);
  const b = new ReservationStore(base, "b", 500);
  await Promise.all([a.initialize(), b.initialize()]);
  try {
    for (let i = 0; i < 100; i += 1) {
      const root = path.join(base, `root-${i}`);
      fs.mkdirSync(root);
      const results = await Promise.all([a.acquire(lane(root, "a")), b.acquire(lane(root, "b"))]);
      assert.equal(results.filter((result) => !("conflict" in result)).length, 1);
      const winner = results.find((result) => !("conflict" in result));
      await a
        .request("release", { authorityKey: winner.authorityKey, claimToken: winner.claimToken })
        .catch(() =>
          b.request("release", {
            authorityKey: winner.authorityKey,
            claimToken: winner.claimToken,
          }),
        );
    }
  } finally {
    await Promise.allSettled([a.terminate(), b.terminate()]);
    fs.rmSync(base, { recursive: true, force: true });
  }
});

test("dead worker fails closed without releasing durable state", () =>
  fixture(async ({ store }) => {
    await store.terminate();
    await assert.rejects(store.request("list"), /RESERVATION_WORKER/);
  }));
