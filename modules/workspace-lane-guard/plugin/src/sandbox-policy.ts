import { boundedCode, type TargetConfig } from "./lane-schema.ts";

export async function attestSandbox(
  api: any,
  target: TargetConfig,
  workspaceRoot: string,
  probeSessionKey: string,
): Promise<void> {
  const result = await api.runtime.sandbox.prepareWorkspaceAuthority({
    config: api.config,
    agentId: target.agentId,
    sessionKey: probeSessionKey,
    workspaceDir: workspaceRoot,
    requiredToolNames: target.tools,
    confinedToolNames: [],
  });
  if (!result?.sandboxed) throw new Error("SANDBOX_REQUIRED");
  if (result.workspaceAccess !== target.access) throw new Error("SANDBOX_ACCESS_MISMATCH");
  if (result.confinementError) {
    throw new Error(`SANDBOX_CONFINEMENT_ERROR:${boundedCode(result.confinementError)}`);
  }
}
