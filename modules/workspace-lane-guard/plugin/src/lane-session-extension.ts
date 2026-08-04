import type { LaneRecord } from "./lane-schema.ts";

export function registerLaneExtension(
  api: any,
  onCleanup: (sessionKey: string | undefined, reason: string) => Promise<void>,
): void {
  api.session.state.registerSessionExtension({
    namespace: "lane",
    description: "Authenticated, expiring workspace delegation lane",
    project: ({ state }: { state: unknown }) => state as LaneRecord | undefined,
    sessionEntrySlotKey: "workspaceLane",
    cleanup: ({ sessionKey, reason }: { sessionKey?: string; reason: string }) =>
      onCleanup(sessionKey, reason),
  });
}
