import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import {
  MAX_LANE_TTL_MS,
  SUPPORTED_OPENCLAW_PACKAGE,
  SUPPORTED_OPENCLAW_VERSION,
  parseGuardConfig,
  parseLane,
  validateSpawnParams,
} from "../../plugin/src/lane-schema.ts";
import {
  canonicalWorkspace,
  isInside,
  relationship,
  validateConfiguredRoots,
  validateTaskRoot,
} from "../../plugin/src/path-policy.ts";
import {
  assertReviewedGlobalSubagentConfig,
  assertTargetConfigMatchesHost,
  assertTrustedToolPolicyIsolation,
  configFingerprint,
  readGlobalSubagentConfig,
  workspaceFingerprint,
} from "../../plugin/src/fingerprint.ts";
import { attestSandbox } from "../../plugin/src/sandbox-policy.ts";
import { boundedStatus } from "../../plugin/src/status.ts";
import { createReadinessHandler } from "../../plugin/src/readiness.ts";
import {
  isActiveTask,
  isTerminalTask,
  parseAcceptedSpawnResult,
} from "../../plugin/src/task-adapter.ts";

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

function withRoots(run) {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), "wlg-path-"));
  const a = path.join(base, "a");
  const b = path.join(base, "b");
  fs.mkdirSync(a);
  fs.mkdirSync(b);
  try {
    return run({ base, a, b });
  } finally {
    fs.rmSync(base, { recursive: true, force: true });
  }
}

test("guard config accepts finite explicit targets", () =>
  withRoots(({ a }) => {
    const parsed = parseGuardConfig({
      stateDir: a,
      openclawVersion: SUPPORTED_OPENCLAW_VERSION,
      targets: [
        {
          agentId: "worker",
          workspaceRoot: a,
          access: "rw",
          tools: ["exec"],
          model: "gpt-5",
          thinking: "extra-high",
        },
      ],
    });
    assert.equal(parsed.acquisitionDeadlineMs, 500);
    assert.equal(parsed.targets.length, 1);
  }));

test("guard config rejects unknown and duplicate target fields", () => {
  assert.throws(
    () => parseGuardConfig({ stateDir: "/x", openclawVersion: "x", nope: true, targets: [] }),
    /UNKNOWN_CONFIG_KEY/,
  );
  assert.throws(
    () =>
      parseGuardConfig({
        stateDir: "/x",
        openclawVersion: SUPPORTED_OPENCLAW_VERSION,
        targets: [
          {
            agentId: "w",
            workspaceRoot: "/x",
            access: "ro",
            tools: ["exec"],
            model: "m",
            thinking: "extra-high",
          },
          {
            agentId: "w",
            workspaceRoot: "/y",
            access: "ro",
            tools: ["exec"],
            model: "m",
            thinking: "extra-high",
          },
        ],
      }),
    /DUPLICATE_AGENT_ID/,
  );
});

test("guard config rejects every non-exact OpenClaw version", () => {
  assert.throws(
    () =>
      parseGuardConfig({
        stateDir: "/x",
        openclawVersion: "2026.7.1-2",
        targets: [
          {
            agentId: "w",
            workspaceRoot: "/x",
            access: "ro",
            tools: ["read"],
            model: "m",
            thinking: "extra-high",
          },
        ],
      }),
    /UNSUPPORTED_OPENCLAW_VERSION/,
  );
});

test("trusted policy activation requires an explicit main/Codex tool policy", () => {
  assert.doesNotThrow(() => assertTrustedToolPolicyIsolation(globalConfig));
  const codexImplicit = structuredClone(globalConfig);
  codexImplicit.plugins = {
    allow: ["codex", "workspace-lane-guard"],
    entries: { codex: { enabled: true } },
  };
  assert.throws(
    () => assertTrustedToolPolicyIsolation(codexImplicit),
    /CODEX_TRUSTED_TOOL_POLICY_ISOLATION_REQUIRED/,
  );

  const explicitExec = structuredClone(codexImplicit);
  explicitExec.tools = { exec: { mode: "auto" } };
  assert.doesNotThrow(() => assertTrustedToolPolicyIsolation(explicitExec));

  const explicitCodex = structuredClone(codexImplicit);
  explicitCodex.plugins.entries.codex.config = {
    appServer: { approvalPolicy: "never" },
  };
  assert.doesNotThrow(() => assertTrustedToolPolicyIsolation(explicitCodex));
});

test("lane TTL is strict and restart-neutral", () => {
  const now = Date.now();
  const lane = {
    schemaVersion: 3,
    parentSessionKey: "s",
    parentAgentId: "p",
    targetAgentId: "t",
    workspaceRoot: "/w",
    authorityRoot: "/w",
    taskRoot: "/w/t",
    access: "rw",
    issuedAt: now,
    expiresAt: now + 60_000,
    issuerAuthority: "operator.admin",
    issuanceId: "i",
    configFingerprint: "c",
    workspaceFingerprint: "w",
    openclawVersion: SUPPORTED_OPENCLAW_VERSION,
    pluginVersion: "0.1.0",
  };
  assert.equal(parseLane(lane, now).issuanceId, "i");
  assert.throws(() => parseLane({ ...lane, expiresAt: now }, now), /LANE_TTL_INVALID|LANE_EXPIRED/);
  assert.throws(
    () => parseLane({ ...lane, expiresAt: now + MAX_LANE_TTL_MS + 1 }, now),
    /LANE_TTL_INVALID/,
  );
  assert.throws(() => parseLane({ ...lane, extra: true }, now), /LANE_SCHEMA_MISMATCH/);
});

test("spawn schema accepts only caller-owned task fields", () => {
  assert.deepEqual(validateSpawnParams({ task: "do", taskName: "task_1", label: "label" }), {
    task: "do",
    taskName: "task_1",
    label: "label",
  });
  for (const key of ["agentId", "cwd", "runtime", "mode", "cleanup", "sandbox"]) {
    assert.throws(
      () => validateSpawnParams({ task: "do", [key]: "x" }),
      /CALLER_OVERRIDE_REJECTED/,
    );
  }
  for (const key of [
    "model",
    "thinking",
    "context",
    "visible",
    "thread",
    "attachments",
    "worktree",
    "unknown",
  ]) {
    assert.throws(() => validateSpawnParams({ task: "do", [key]: "x" }), /UNSUPPORTED_SPAWN_KEY/);
  }
});

test("component relationships do not confuse lexical prefixes", () => {
  assert.equal(relationship(["root", "a"], ["root", "a", "b"]), "ancestor");
  assert.equal(relationship(["root", "a", "b"], ["root", "a"]), "descendant");
  assert.equal(relationship(["root", "a"], ["root", "ab"]), "disjoint");
  assert.equal(isInside("/tmp/work/a", "/tmp/work"), true);
  assert.equal(isInside("/tmp/worker", "/tmp/work"), false);
});

test("configured strict ancestry is rejected; reviewed ro/rw duplicate is accepted", () =>
  withRoots(({ a }) => {
    const nested = path.join(a, "nested");
    fs.mkdirSync(nested);
    const base = { tools: ["exec"], model: "m", thinking: "extra-high" };
    assert.throws(
      () =>
        validateConfiguredRoots([
          { ...base, agentId: "a", workspaceRoot: a, access: "rw" },
          { ...base, agentId: "b", workspaceRoot: nested, access: "ro" },
        ]),
      /ANCESTRY/,
    );
    const roots = validateConfiguredRoots([
      { ...base, agentId: "a", workspaceRoot: a, access: "rw", sameRootPair: "pair" },
      { ...base, agentId: "b", workspaceRoot: a, access: "ro", sameRootPair: "pair" },
    ]);
    assert.equal(roots.get("a").root, roots.get("b").root);
  }));

test("task roots reject lexical and symlink escapes", () =>
  withRoots(({ a, b }) => {
    assert.equal(
      validateTaskRoot(path.join(a, "future", "task"), a),
      path.join(a, "future", "task"),
    );
    assert.throws(() => validateTaskRoot(b, a), /TASK_ROOT_ESCAPE/);
    fs.symlinkSync(b, path.join(a, "link"));
    assert.throws(() => validateTaskRoot(path.join(a, "link", "task"), a), /SYMLINK/);
  }));

test("workspace fingerprint changes with inode replacement", () =>
  withRoots(({ a }) => {
    const first = workspaceFingerprint(canonicalWorkspace(a));
    fs.renameSync(a, `${a}-old`);
    fs.mkdirSync(a);
    const second = workspaceFingerprint(canonicalWorkspace(a));
    assert.notEqual(first, second);
  }));

test("config fingerprint covers reviewed global subagent values", () =>
  withRoots(({ a }) => {
    const config = parseGuardConfig({
      stateDir: a,
      openclawVersion: SUPPORTED_OPENCLAW_VERSION,
      targets: [
        {
          agentId: "worker",
          workspaceRoot: a,
          access: "rw",
          tools: ["exec"],
          model: "m",
          thinking: "extra-high",
        },
      ],
    });
    const roots = validateConfiguredRoots(config.targets);
    const one = configFingerprint(config, roots, readGlobalSubagentConfig(globalConfig));
    const changed = structuredClone(globalConfig);
    changed.agents.defaults.subagents.archiveAfterMinutes = 61;
    const two = configFingerprint(config, roots, readGlobalSubagentConfig(changed));
    assert.notEqual(one, two);
    assertReviewedGlobalSubagentConfig(globalConfig);
    assert.throws(
      () => assertReviewedGlobalSubagentConfig(changed),
      /GLOBAL_SUBAGENT_CONFIG_DRIFT/,
    );
  }));

test("host target config must match workspace, model, thinking, sandbox, and tools", () =>
  withRoots(({ a }) => {
    const target = {
      agentId: "worker",
      workspaceRoot: a,
      access: "rw",
      tools: ["exec"],
      model: "m",
      thinking: "extra-high",
    };
    const roots = validateConfiguredRoots([target]);
    const host = structuredClone(globalConfig);
    host.agents.entries = {
      main: { subagents: { allowAgents: ["worker"] } },
      worker: {
        workspace: a,
        model: "m",
        thinkingDefault: "xhigh",
        sandbox: { mode: "all", workspaceAccess: "rw", scope: "session" },
        tools: { allow: ["exec"], sandbox: { tools: { allow: ["exec"] } } },
      },
    };
    assertTargetConfigMatchesHost(host, [target], roots);
    host.agents.entries.worker.sandbox.scope = "agent";
    assert.throws(
      () => assertTargetConfigMatchesHost(host, [target], roots),
      /TARGET_SANDBOX_CONFIG_DRIFT/,
    );
    host.agents.entries.worker.sandbox.scope = "session";
    host.agents.entries.worker.tools.allow = ["exec", "gateway"];
    assert.throws(
      () => assertTargetConfigMatchesHost(host, [target], roots),
      /TARGET_TOOL_CONFIG_DRIFT/,
    );
    host.agents.entries.main.subagents.allowAgents = ["*"];
    assert.throws(
      () => assertTargetConfigMatchesHost(host, [target], roots),
      /WILDCARD_SUBAGENT_ALLOWLIST/,
    );
  }));

test("sandbox attestation accepts only exact reviewed access", async () => {
  const target = { agentId: "worker", access: "ro", tools: ["read"] };
  const api = {
    config: {},
    runtime: {
      sandbox: {
        prepareWorkspaceAuthority: async () => ({
          sandboxed: true,
          workspaceAccess: "ro",
          confinementError: null,
        }),
      },
    },
  };
  await attestSandbox(api, target, "/work", "probe");
  api.runtime.sandbox.prepareWorkspaceAuthority = async () => ({
    sandboxed: false,
    workspaceAccess: "ro",
  });
  await assert.rejects(attestSandbox(api, target, "/work", "probe"), /SANDBOX_REQUIRED/);
});

test("bounded status truncates and excludes claim tokens", () => {
  const rows = Array.from({ length: 3 }, (_, i) => ({
    authority_root: `/w/${i}`,
    access: "rw",
    state: "bound",
    target_agent_id: "w",
    native_task_id: `t${i}`,
    run_id: `r${i}`,
    child_session_key: `c${i}`,
    updated_at: i,
    claim_token: "secret",
  }));
  const result = boundedStatus(rows, 2);
  assert.equal(result.truncated, true);
  assert.equal(result.reservations.length, 2);
  assert.deepEqual(result.qualifiedOpenclawPackage, {
    ...SUPPORTED_OPENCLAW_PACKAGE,
    enforcement: "exact-version-only",
  });
  assert.equal(JSON.stringify(result).includes("secret"), false);
});

test("task parsing and terminal spellings match native presentations", () => {
  assert.deepEqual(
    parseAcceptedSpawnResult({
      content: [{ text: JSON.stringify({ runId: "r", childSessionKey: "c" }) }],
    }),
    { runId: "r", childSessionKey: "c" },
  );
  assert.equal(isActiveTask({ status: "running" }), true);
  for (const status of ["succeeded", "failed", "timed-out", "timed_out", "cancelled", "canceled"]) {
    assert.equal(isTerminalTask({ status }), true);
  }
});

test("readiness is false for an incoherent generation and rejects replay", async () => {
  const reconciler = { ready: true, generationId: "g", lastReconciledAt: 20, lastErrorCode: null };
  const handler = createReadinessHandler({
    gatewayGenerationId: "g",
    gatewayStartedAt: 10,
    policyRegistered: true,
    workerReady: true,
    reconciler,
    store: { request: async () => ({ ready: true, generationId: "g" }) },
  });
  const ctx = { payload: { challenge: "abcdefghijklmnop" } };
  const first = await handler(ctx);
  assert.equal(first.result.ready, true);
  assert.equal((await handler(ctx)).code, "REPLAYED_CHALLENGE");
  reconciler.generationId = "old";
  const second = await handler({ payload: { challenge: "ponmlkjihgfedcba" } });
  assert.equal(second.result.ready, false);
});

test("readiness stays closed through restart recovery and reports the recovered generation", async () => {
  const startedAt = Date.now();
  const reconciler = {
    ready: false,
    generationId: "generation-2",
    lastReconciledAt: 0,
    lastErrorCode: null,
  };
  let workerGeneration = "generation-2";
  const handler = createReadinessHandler({
    gatewayGenerationId: "generation-2",
    gatewayStartedAt: startedAt,
    policyRegistered: true,
    workerReady: true,
    reconciler,
    store: { request: async () => ({ ready: true, generationId: workerGeneration }) },
  });

  const recovering = await handler({ payload: { challenge: "restartrecovering" } });
  assert.equal(recovering.result.ready, false);
  assert.equal(recovering.result.reconcilerReady, false);
  assert.equal(recovering.result.gatewayGenerationId, "generation-2");

  reconciler.ready = true;
  reconciler.lastReconciledAt = startedAt + 1;
  const recovered = await handler({ payload: { challenge: "restartrecovered1" } });
  assert.equal(recovered.result.ready, true);
  assert.equal(recovered.result.workerReady, true);
  assert.equal(recovered.result.reconcilerReady, true);
  assert.equal(recovered.result.gatewayGenerationId, "generation-2");

  workerGeneration = "generation-1";
  const staleWorker = await handler({ payload: { challenge: "restartstalework" } });
  assert.equal(staleWorker.result.ready, false);
  assert.equal(staleWorker.result.workerReady, false);
});
