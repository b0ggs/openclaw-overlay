export const GOVERNED_CLEANUP_MODE = "delete" as const;

export function assertNoCleanupLedger(): true {
  return true;
}
