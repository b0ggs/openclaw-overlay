import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import plugin from "../../plugin/src/index.ts";

const RUNTIME_KEY = Symbol.for("openclaw.workspace-lane-guard.runtime.v1");

function hostConfig(workspaceRoot) {
  return {
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
      entries: {
        main: { subagents: { allowAgents: ["worker"] } },
        worker: {
          workspace: workspaceRoot,
          model: "m",
          thinkingDefault: "xhigh",
          sandbox: { mode: "all", workspaceAccess: "rw", scope: "session" },
          tools: { allow: ["exec"], sandbox: { tools: { allow: ["exec"] } } },
        },
      },
    },
  };
}

function registration(base, workspaceRoot) {
  const services = [];
  const hooks = new Map();
  const lifecycles = [];
  const api = {
    pluginConfig: {
      stateDir: path.join(base, "state"),
      openclawVersion: "2026.7.2-beta.4",
      targets: [
        {
          agentId: "worker",
          workspaceRoot,
          access: "rw",
          tools: ["exec"],
          model: "m",
          thinking: "extra-high",
        },
      ],
    },
    config: hostConfig(workspaceRoot),
    runtime: {
      sandbox: {
        prepareWorkspaceAuthority: async () => ({ sandboxed: true, workspaceAccess: "rw" }),
      },
      tasks: { runs: { bindSession: () => ({ list: () => [] }) } },
    },
    logger: { error: () => {} },
    session: {
      state: { registerSessionExtension: () => {} },
      controls: { registerSessionAction: () => {} },
    },
    lifecycle: { registerRuntimeLifecycle: (entry) => lifecycles.push(entry) },
    registerTrustedToolPolicy: () => {},
    registerService: (service) => services.push(service),
    on: (name, handler) => {
      if (!hooks.has(name)) hooks.set(name, []);
      hooks.get(name).push(handler);
    },
  };
  plugin.register(api);
  assert.equal(services.length, 1);
  assert.equal(lifecycles.length, 1);
  return { service: services[0], hooks, lifecycle: lifecycles[0] };
}

test("runtime reload shares one worker and timer, then replaces the unhealthy generation", async () => {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), "wlg-runtime-restart-"));
  const workspaceRoot = path.join(base, "workspace");
  fs.mkdirSync(workspaceRoot, { recursive: true });
  delete globalThis[RUNTIME_KEY];
  const first = registration(base, workspaceRoot);
  const second = registration(base, workspaceRoot);
  const firstGatewayStart = first.hooks.get("gateway_start")[0];
  const secondGatewayStart = second.hooks.get("gateway_start")[0];
  const firstGatewayStop = first.hooks.get("gateway_stop")[0];
  const secondGatewayStop = second.hooks.get("gateway_stop")[0];
  try {
    await Promise.all([firstGatewayStart(), secondGatewayStart()]);
    const shared = globalThis[RUNTIME_KEY];
    assert.equal(shared.owners.size, 2);
    assert.ok(shared.readiness.store);
    assert.ok(shared.reconcileTimer);
    assert.equal(shared.readiness.workerReady, true);
    assert.equal(shared.reconciler.ready, true);
    const firstStore = shared.readiness.store;
    const firstTimer = shared.reconcileTimer;
    const firstGeneration = shared.readiness.gatewayGenerationId;

    await first.service.start();
    assert.equal(shared.readiness.store, firstStore);
    assert.equal(shared.reconcileTimer, firstTimer);
    assert.equal(shared.readiness.gatewayGenerationId, firstGeneration);

    shared.reconciler.ready = false;
    const restarting = firstGatewayStart();
    assert.equal(shared.readiness.workerReady, false);
    assert.equal(shared.reconciler.ready, false);
    assert.equal(shared.admission.workerReady, false);
    assert.equal(shared.admission.reconcilerReady, false);
    assert.equal(shared.readiness.store, null);
    await restarting;
    assert.notEqual(shared.readiness.store, firstStore);
    assert.notEqual(shared.reconcileTimer, firstTimer);
    assert.notEqual(shared.readiness.gatewayGenerationId, firstGeneration);
    assert.equal(firstTimer._destroyed, true);
    assert.equal(shared.owners.size, 2);
    assert.equal(shared.readiness.workerReady, true);
    assert.equal(shared.reconciler.ready, true);
    assert.equal(
      (await shared.readiness.store.request("ping")).generationId,
      shared.readiness.gatewayGenerationId,
    );

    const recoveredStore = shared.readiness.store;
    const recoveredTimer = shared.reconcileTimer;
    await firstGatewayStop();
    assert.equal(shared.owners.size, 1);
    assert.equal(shared.readiness.store, recoveredStore);
    assert.equal(shared.reconcileTimer, recoveredTimer);
    await secondGatewayStop();
    assert.equal(shared.owners.size, 0);
    assert.equal(shared.readiness.store, null);
    assert.equal(shared.reconcileTimer, null);
    assert.equal(recoveredTimer._destroyed, true);
  } finally {
    await Promise.allSettled([first.service.stop(), second.service.stop()]);
    delete globalThis[RUNTIME_KEY];
    fs.rmSync(base, { recursive: true, force: true });
  }
});
