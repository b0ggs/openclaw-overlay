import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { createAdmissionPolicy } from "../../plugin/src/sessions-spawn-policy.ts";
import {
  configFingerprint,
  readGlobalSubagentConfig,
  workspaceFingerprint,
} from "../../plugin/src/fingerprint.ts";
import { canonicalWorkspace, validateConfiguredRoots } from "../../plugin/src/path-policy.ts";

const globalConfig = {
  agents: {
    defaults: {
      subagents: {
        requireAgentId: true,
        maxSpawnDepth: 1,
        maxConcurrent: 4,
        maxChildrenPerAgent: 2,
        runTimeoutSeconds: 14400,
        archiveAfterMinutes: 60,
      },
    },
  },
};

function setup() {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), "wlg-admit-"));
  const target = {
    agentId: "worker",
    workspaceRoot: base,
    access: "rw",
    tools: ["exec"],
    model: "m",
    thinking: "extra-high",
  };
  const config = {
    stateDir: base,
    openclawVersion: "2026.7.2-beta.4",
    acquisitionDeadlineMs: 500,
    targets: [target],
  };
  const hostConfig = structuredClone(globalConfig);
  hostConfig.agents.entries = {
    main: { subagents: { allowAgents: ["worker"] } },
    worker: {
      workspace: base,
      model: "m",
      thinkingDefault: "xhigh",
      sandbox: { mode: "all", workspaceAccess: "rw", scope: "session" },
      tools: { allow: ["exec"], sandbox: { tools: { allow: ["exec"] } } },
    },
  };
  const roots = validateConfiguredRoots(config.targets);
  const lane = {
    schemaVersion: 3,
    parentSessionKey: "parent",
    parentAgentId: "main",
    targetAgentId: "worker",
    workspaceRoot: base,
    authorityRoot: base,
    taskRoot: base,
    access: "rw",
    issuedAt: Date.now(),
    expiresAt: Date.now() + 60_000,
    issuerAuthority: "operator.admin",
    issuanceId: crypto.randomUUID(),
    configFingerprint: configFingerprint(config, roots, readGlobalSubagentConfig(hostConfig)),
    workspaceFingerprint: workspaceFingerprint(canonicalWorkspace(base)),
    openclawVersion: config.openclawVersion,
    pluginVersion: "0.1.0",
  };
  const claims = [];
  const store = {
    acquire: async () => {
      const claim = {
        authorityKey: "key",
        authorityRoot: base,
        claimToken: crypto.randomUUID(),
        conflictId: "conflict",
      };
      claims.push(claim);
      return claim;
    },
    quarantine: async () => {},
  };
  const api = {
    config: hostConfig,
    runtime: {
      sandbox: {
        prepareWorkspaceAuthority: async () => ({
          sandboxed: true,
          workspaceAccess: "rw",
          confinementError: null,
        }),
      },
    },
  };
  const state = {
    generationId: "generation",
    policyRegistered: true,
    workerReady: true,
    reconcilerReady: true,
    store,
    roots,
    configFingerprint: lane.configFingerprint,
    pending: new Map(),
  };
  const ctx = {
    sessionKey: "parent",
    agentId: "main",
    runId: "run",
    toolCallId: "call",
    getSessionExtension: () => lane,
  };
  return { base, target, config, lane, store, api, state, ctx, claims };
}

test("trusted admission forces exact native parameters and records provisional correlation", async () => {
  const f = setup();
  try {
    const result = await createAdmissionPolicy(f.api, f.config, f.state).evaluate(
      { toolName: "sessions_spawn", params: { task: "work", taskName: "job", label: "label" } },
      f.ctx,
    );
    assert.deepEqual(result.params, {
      task: "work",
      taskName: "job",
      label: "label",
      agentId: "worker",
      cwd: f.base,
      runtime: "subagent",
      mode: "run",
      cleanup: "delete",
      sandbox: "require",
    });
    assert.equal(f.state.pending.size, 1);
  } finally {
    fs.rmSync(f.base, { recursive: true, force: true });
  }
});

test("readiness state gates admission before reservation acquisition", async () => {
  const unavailable = [
    ["generationId", null],
    ["policyRegistered", false],
    ["workerReady", false],
    ["reconcilerReady", false],
    ["store", null],
  ];
  for (const [field, value] of unavailable) {
    const f = setup();
    try {
      f.state[field] = value;
      const result = await createAdmissionPolicy(f.api, f.config, f.state).evaluate(
        { toolName: "sessions_spawn", params: { task: "work" } },
        f.ctx,
      );
      assert.equal(result.block, true);
      assert.match(result.blockReason, /AUTHORITY_ROOT_QUARANTINE:PLUGIN_NOT_READY/);
      assert.equal(f.claims.length, 0);
      assert.equal(f.state.pending.size, 0);
    } finally {
      fs.rmSync(f.base, { recursive: true, force: true });
    }
  }
});

test("trusted admission ignores unrelated headless main and Codex harness tools", async () => {
  const f = setup();
  try {
    f.state.workerReady = false;
    f.state.reconcilerReady = false;
    const policy = createAdmissionPolicy(f.api, f.config, f.state);
    for (const toolName of ["exec", "read", "apply_patch", "session_status"]) {
      const startedAt = Date.now();
      assert.equal(await policy.evaluate({ toolName, params: {} }, f.ctx), undefined);
      assert.ok(Date.now() - startedAt < 50);
    }
  } finally {
    fs.rmSync(f.base, { recursive: true, force: true });
  }
});

test("trusted admission blocks caller overrides, missing lane, and sandbox failure", async () => {
  const f = setup();
  try {
    const policy = createAdmissionPolicy(f.api, f.config, f.state);
    let result = await policy.evaluate(
      { toolName: "sessions_spawn", params: { task: "work", model: "x" } },
      f.ctx,
    );
    assert.match(result.blockReason, /UNSUPPORTED_SPAWN_KEY/);
    result = await policy.evaluate(
      { toolName: "sessions_spawn", params: { task: "work" } },
      { ...f.ctx, getSessionExtension: () => undefined },
    );
    assert.match(result.blockReason, /LANE_MISSING/);
    f.api.runtime.sandbox.prepareWorkspaceAuthority = async () => ({
      sandboxed: false,
      workspaceAccess: "rw",
    });
    result = await policy.evaluate({ toolName: "sessions_spawn", params: { task: "work" } }, f.ctx);
    assert.match(result.blockReason, /SANDBOX_REQUIRED/);
  } finally {
    fs.rmSync(f.base, { recursive: true, force: true });
  }
});

test("trusted admission redacts foreign holder task ids from bounded conflicts", async () => {
  const f = setup();
  f.store.acquire = async () => ({
    conflict: {
      code: "AUTHORITY_TREE_RESERVED",
      authorityRoot: f.base,
      conflictId: "opaque",
      holderTaskId: "task",
      sameControllingSession: false,
    },
  });
  try {
    const result = await createAdmissionPolicy(f.api, f.config, f.state).evaluate(
      { toolName: "sessions_spawn", params: { task: "work" } },
      f.ctx,
    );
    assert.equal(result.block, true);
    assert.match(result.blockReason, /AUTHORITY_TREE_RESERVED/);
    assert.equal(result.blockReason.includes("claimToken"), false);
    assert.equal(result.blockReason.includes("holderTaskId"), false);
    assert.equal(result.blockReason.includes("task"), false);
    assert.ok(result.blockReason.length < 1000);
  } finally {
    fs.rmSync(f.base, { recursive: true, force: true });
  }
});

test("trusted admission reveals a holder task id only to the same controlling session", async () => {
  const f = setup();
  f.store.acquire = async () => ({
    conflict: {
      code: "AUTHORITY_TREE_RESERVED",
      authorityRoot: f.base,
      conflictId: "opaque",
      holderTaskId: "same-session-task",
      sameControllingSession: true,
    },
  });
  try {
    const result = await createAdmissionPolicy(f.api, f.config, f.state).evaluate(
      { toolName: "sessions_spawn", params: { task: "work" } },
      f.ctx,
    );
    assert.equal(result.block, true);
    assert.match(result.blockReason, /"holderTaskId":"same-session-task"/);
  } finally {
    fs.rmSync(f.base, { recursive: true, force: true });
  }
});

test("final path and fingerprint revalidation rejects replacement drift", async () => {
  const f = setup();
  let calls = 0;
  f.api.runtime.sandbox.prepareWorkspaceAuthority = async () => {
    calls += 1;
    fs.renameSync(f.base, `${f.base}-old`);
    fs.mkdirSync(f.base);
    return { sandboxed: true, workspaceAccess: "rw", confinementError: null };
  };
  const result = await createAdmissionPolicy(f.api, f.config, f.state).evaluate(
    { toolName: "sessions_spawn", params: { task: "work" } },
    f.ctx,
  );
  assert.equal(calls, 1);
  assert.match(result.blockReason, /WORKSPACE_FINGERPRINT_DRIFT/);
  fs.rmSync(f.base, { recursive: true, force: true });
  fs.rmSync(`${f.base}-old`, { recursive: true, force: true });
});
