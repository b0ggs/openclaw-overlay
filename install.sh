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
  set -- policy-pack prompt-pack heartbeat-progress
fi

for module in "$@"; do
  case "$module" in
    policy-pack)
      bash "$ROOT/modules/policy-pack/install.sh" "$TARGET"
      ;;
    prompt-pack)
      bash "$ROOT/modules/prompt-pack/install.sh" "$TARGET"
      ;;
    heartbeat-progress)
      bash "$ROOT/modules/heartbeat-progress/install.sh" "$TARGET"
      ;;
    *)
      echo "unknown module: $module" >&2
      exit 2
      ;;
  esac
done
