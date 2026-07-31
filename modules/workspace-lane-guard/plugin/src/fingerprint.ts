import crypto from "node:crypto";
import type { CanonicalWorkspace } from "./path-policy.ts";
import {
  PLUGIN_VERSION,
  POLICY_ID,
  LANE_SCHEMA_VERSION,
  SUPPORTED_OPENCLAW_PACKAGE,
  type GuardConfig,
  type TargetConfig,
} from "./lane-schema.ts";
import { canonicalWorkspace, relationship } from "./path-policy.ts";

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

export function sha256(value: unknown): string {
  return `sha256:${crypto.createHash("sha256").update(canonical(value)).digest("hex")}`;
}

export function workspaceFingerprint(workspace: CanonicalWorkspace): string {
  return sha256({
    path: workspace.root,
    device: String(workspace.device),
    inode: String(workspace.inode),
  });
}

export function configFingerprint(
  config: GuardConfig,
  roots: Map<string, CanonicalWorkspace>,
  globalSubagents: Record<string, unknown>,
): string {
  const targets = config.targets
    .map((target: TargetConfig) => ({
      agentId: target.agentId,
      workspaceRoot: roots.get(target.agentId)!.root,
      access: target.access,
      tools: [...target.tools].sort(),
      model: target.model,
      thinking: target.thinking,
      sameRootPair: target.sameRootPair ?? null,
      sandbox: {
        mode: "all",
        scope: "session",
        workspaceAccess: target.access,
      },
      network: "deny-by-target-config",
      browser: "deny-by-target-config",
      elevated: false,
    }))
    .sort((a, b) => a.agentId.localeCompare(b.agentId));
  const relationships = targets.flatMap((left, index) =>
    targets.slice(index + 1).map((right) => ({
      left: left.agentId,
      right: right.agentId,
      relationship: relationship(
        roots.get(left.agentId)!.components,
        roots.get(right.agentId)!.components,
      ),
    })),
  );
  return sha256({
    openclawVersion: config.openclawVersion,
    openclawPackage: SUPPORTED_OPENCLAW_PACKAGE,
    pluginVersion: PLUGIN_VERSION,
    policyId: POLICY_ID,
    laneSchemaVersion: LANE_SCHEMA_VERSION,
    targets,
    relationships,
    maxSpawnDepth: globalSubagents.maxSpawnDepth,
    maxConcurrent: globalSubagents.maxConcurrent,
    maxChildrenPerAgent: globalSubagents.maxChildrenPerAgent,
    requireAgentId: globalSubagents.requireAgentId,
    runTimeoutSeconds: globalSubagents.runTimeoutSeconds,
    archiveAfterMinutes: globalSubagents.archiveAfterMinutes,
    cleanup: "delete",
    cumulativeTokenCostCap: null,
  });
}

export function assertReviewedGlobalSubagentConfig(config: Record<string, unknown>): void {
  const defaults = ((config.agents as Record<string, unknown> | undefined)?.defaults ??
    {}) as Record<string, unknown>;
  const subagents = (defaults.subagents ?? {}) as Record<string, unknown>;
  const expected: Record<string, unknown> = {
    requireAgentId: true,
    maxSpawnDepth: 1,
    maxConcurrent: 4,
    maxChildrenPerAgent: 2,
    runTimeoutSeconds: 14400,
    archiveAfterMinutes: 60,
  };
  for (const [key, value] of Object.entries(expected)) {
    if (subagents[key] !== value) throw new Error(`GLOBAL_SUBAGENT_CONFIG_DRIFT:${key}`);
  }
}

export function readGlobalSubagentConfig(config: Record<string, unknown>): Record<string, unknown> {
  const defaults = ((config.agents as Record<string, unknown> | undefined)?.defaults ??
    {}) as Record<string, unknown>;
  return (defaults.subagents ?? {}) as Record<string, unknown>;
}

function sortedStrings(value: unknown): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) return [];
  return [...value].sort();
}

function normalizedThinking(value: unknown): string {
  return value === "xhigh" ? "extra-high" : String(value ?? "");
}

export function assertTargetConfigMatchesHost(
  hostConfig: Record<string, unknown>,
  targets: TargetConfig[],
  roots: Map<string, CanonicalWorkspace>,
): void {
  const agents = (hostConfig.agents ?? {}) as Record<string, unknown>;
  const entries = (agents.entries ?? {}) as Record<string, unknown>;
  for (const entryValue of Object.values(entries)) {
    const allowAgents = (
      (entryValue as Record<string, unknown>)?.subagents as Record<string, unknown> | undefined
    )?.allowAgents;
    if (Array.isArray(allowAgents) && allowAgents.includes("*"))
      throw new Error("WILDCARD_SUBAGENT_ALLOWLIST_REJECTED");
  }
  for (const target of targets) {
    const entry = entries[target.agentId] as Record<string, unknown> | undefined;
    if (!entry) throw new Error(`TARGET_AGENT_CONFIG_MISSING:${target.agentId}`);
    const actualRoot = canonicalWorkspace(String(entry.workspace ?? "")).root;
    if (actualRoot !== roots.get(target.agentId)?.root)
      throw new Error(`TARGET_WORKSPACE_CONFIG_DRIFT:${target.agentId}`);
    const model =
      typeof entry.model === "string"
        ? entry.model
        : String((entry.model as Record<string, unknown> | undefined)?.primary ?? "");
    if (model !== target.model) throw new Error(`TARGET_MODEL_CONFIG_DRIFT:${target.agentId}`);
    if (normalizedThinking(entry.thinkingDefault) !== normalizedThinking(target.thinking)) {
      throw new Error(`TARGET_THINKING_CONFIG_DRIFT:${target.agentId}`);
    }
    const sandbox = (entry.sandbox ?? {}) as Record<string, unknown>;
    if (
      sandbox.mode !== "all" ||
      sandbox.scope !== "session" ||
      sandbox.workspaceAccess !== target.access
    ) {
      throw new Error(`TARGET_SANDBOX_CONFIG_DRIFT:${target.agentId}`);
    }
    const tools = (entry.tools ?? {}) as Record<string, unknown>;
    const sandboxTools = ((tools.sandbox ?? {}) as Record<string, unknown>).tools as
      | Record<string, unknown>
      | undefined;
    if (
      JSON.stringify(sortedStrings(tools.allow)) !== JSON.stringify([...target.tools].sort()) ||
      JSON.stringify(sortedStrings(sandboxTools?.allow)) !==
        JSON.stringify([...target.tools].sort())
    )
      throw new Error(`TARGET_TOOL_CONFIG_DRIFT:${target.agentId}`);
  }
}
