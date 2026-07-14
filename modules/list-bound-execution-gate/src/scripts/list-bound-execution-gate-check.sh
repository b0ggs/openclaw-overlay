#!/usr/bin/env bash
set -euo pipefail

# Fail-closed host adapter for Overlay V2 Module 3.
# This keeps launch-path integration small while the canonical checker lives in
# the overlay-v2 module repository.

ISSUE_ID="${1:-}"
MODE="${2:-dispatch}"

if [[ -z "$ISSUE_ID" ]]; then
  echo "usage: list-bound-execution-gate-check.sh <issue-id> [dispatch|worker]" >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=scripts/openclaw-paths.sh
source "$SCRIPT_DIR/openclaw-paths.sh"
ROOT="$OPENCLAW_WORKSPACE_ROOT"

if [[ "${OPENCLAW_OVERLAY_V2_ROOT+x}" == "x" ]]; then
  if [[ -z "$OPENCLAW_OVERLAY_V2_ROOT" ]]; then
    echo "OPENCLAW_OVERLAY_V2_ROOT is set but empty" >&2
    exit 1
  fi
  CHECKER_CLI="$OPENCLAW_OVERLAY_V2_ROOT/modules/list-bound-execution-gate/src/cli.js"
else
  CHECKER_CLI="$SCRIPT_DIR/list-bound-execution-gate/src/cli.js"
fi

if [[ ! -f "$CHECKER_CLI" ]]; then
  echo "list-bound execution gate checker missing: $CHECKER_CLI" >&2
  exit 1
fi

node "$CHECKER_CLI" check --workspace "$ROOT" --issue-id "$ISSUE_ID" --mode "$MODE"
