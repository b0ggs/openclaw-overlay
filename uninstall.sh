#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
TARGET="${1:-$PWD}"
shift || true

if [[ ! -d "$TARGET" ]]; then
  echo "target workspace does not exist: $TARGET" >&2
  exit 2
fi

TARGET="$(cd "$TARGET" && pwd -P)"

if [[ "$#" -eq 0 ]]; then
  set -- supervisor-review-finalizer execution-pattern-router authorization-freeze list-bound-execution-gate heartbeat-progress prompt-pack policy-pack
fi

for module in "$@"; do
  case "$module" in
    policy-pack)
      bash "$ROOT/modules/policy-pack/uninstall.sh" "$TARGET"
      ;;
    prompt-pack)
      bash "$ROOT/modules/prompt-pack/uninstall.sh" "$TARGET"
      ;;
    heartbeat-progress)
      bash "$ROOT/modules/heartbeat-progress/uninstall.sh" "$TARGET"
      ;;
    list-bound-execution-gate)
      bash "$ROOT/modules/list-bound-execution-gate/uninstall.sh" "$TARGET"
      ;;
    authorization-freeze)
      bash "$ROOT/modules/authorization-freeze/uninstall.sh" "$TARGET"
      ;;
    execution-pattern-router)
      bash "$ROOT/modules/execution-pattern-router/uninstall.sh" "$TARGET"
      ;;
    supervisor-review-finalizer)
      bash "$ROOT/modules/supervisor-review-finalizer/uninstall.sh" "$TARGET"
      ;;
    *)
      echo "unknown module: $module" >&2
      exit 2
      ;;
  esac
done
