#!/usr/bin/env bash
set -euo pipefail

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
TARGET="${1:-$PWD}"
OPENCLAW_BIN="${OPENCLAW_BIN:-openclaw}"
EXPECTED_OPENCLAW_VERSION="2026.7.2-beta.4"

unsupported_openclaw_version() {
  local actual="${1:-unavailable}"
  printf 'UNSUPPORTED_OPENCLAW_VERSION expected=%s actual=%s\n' \
    "$EXPECTED_OPENCLAW_VERSION" "$actual" >&2
  exit 3
}

if [[ ! -d "$TARGET" ]]; then
  echo "target OpenClaw state directory does not exist: $TARGET" >&2
  exit 2
fi

TARGET="$(cd "$TARGET" && pwd -P)"

if ! openclaw_version_output="$("$OPENCLAW_BIN" --version 2>/dev/null)"; then
  unsupported_openclaw_version "unavailable"
fi
if [[ ! "$openclaw_version_output" =~ ^OpenClaw[[:space:]]+([^[:space:]]+)([[:space:]]+\([0-9a-fA-F]{7,40}\))?$ ]]; then
  unsupported_openclaw_version "unrecognized"
fi
resolved_openclaw_version="${BASH_REMATCH[1]}"
if [[ "$resolved_openclaw_version" != "$EXPECTED_OPENCLAW_VERSION" ]]; then
  unsupported_openclaw_version "$resolved_openclaw_version"
fi

STATE_DIR="$TARGET/.openclaw-overlay/modules/workspace-lane-guard"
BACKUP_DIR="$STATE_DIR/backup"
MANIFEST="$STATE_DIR/manifest.tsv"
DIRS_CREATED="$STATE_DIR/dirs-created.txt"
CONFIG="$TARGET/openclaw.json"

FILES=(
  "plugin/openclaw.plugin.json|extensions/workspace-lane-guard/openclaw.plugin.json|0644"
  "plugin/package.json|extensions/workspace-lane-guard/package.json|0644"
  "plugin/src/cleanup-support.ts|extensions/workspace-lane-guard/src/cleanup-support.ts|0644"
  "plugin/src/fingerprint.ts|extensions/workspace-lane-guard/src/fingerprint.ts|0644"
  "plugin/src/index.ts|extensions/workspace-lane-guard/src/index.ts|0644"
  "plugin/src/lane-schema.ts|extensions/workspace-lane-guard/src/lane-schema.ts|0644"
  "plugin/src/lane-session-extension.ts|extensions/workspace-lane-guard/src/lane-session-extension.ts|0644"
  "plugin/src/operator-actions.ts|extensions/workspace-lane-guard/src/operator-actions.ts|0644"
  "plugin/src/path-policy.ts|extensions/workspace-lane-guard/src/path-policy.ts|0644"
  "plugin/src/readiness.ts|extensions/workspace-lane-guard/src/readiness.ts|0644"
  "plugin/src/reservation-db.ts|extensions/workspace-lane-guard/src/reservation-db.ts|0644"
  "plugin/src/reservation-reconciler.ts|extensions/workspace-lane-guard/src/reservation-reconciler.ts|0644"
  "plugin/src/reservation-worker.ts|extensions/workspace-lane-guard/src/reservation-worker.ts|0644"
  "plugin/src/sandbox-policy.ts|extensions/workspace-lane-guard/src/sandbox-policy.ts|0644"
  "plugin/src/sessions-spawn-policy.ts|extensions/workspace-lane-guard/src/sessions-spawn-policy.ts|0644"
  "plugin/src/status.ts|extensions/workspace-lane-guard/src/status.ts|0644"
  "plugin/src/task-adapter.ts|extensions/workspace-lane-guard/src/task-adapter.ts|0644"
  "scripts/workspace-lane-control.mjs|scripts/workspace-lane-control.mjs|0755"
  "scripts/lane-guard-healthmon.mjs|scripts/lane-guard-healthmon.mjs|0755"
  "prompt-pack/parent-delegation-guidance.md|prompts/workspace-lane-parent-guidance.md|0644"
)

record_created_parent_dirs() {
  local rel_dir="$1"
  while [[ "$rel_dir" != "." && "$rel_dir" != "/" && -n "$rel_dir" ]]; do
    if [[ ! -d "$TARGET/$rel_dir" ]]; then
      echo "$rel_dir" >> "$DIRS_CREATED.tmp"
    fi
    rel_dir="$(dirname "$rel_dir")"
  done
}

if [[ -f "$MANIFEST" ]]; then
  for entry in "${FILES[@]}"; do
    IFS='|' read -r src_rel dest_rel mode <<< "$entry"
    src="$MODULE_DIR/$src_rel"
    dest="$TARGET/$dest_rel"
    [[ -f "$src" ]] || { echo "module source missing: $src_rel" >&2; exit 1; }
    [[ -f "$dest" ]] || { echo "installed file missing: $dest_rel" >&2; exit 1; }
    cmp -s "$src" "$dest" || { echo "installed file differs from module source: $dest_rel" >&2; exit 1; }
    chmod "$mode" "$dest"
  done
  exit 0
fi

mkdir -p "$BACKUP_DIR"
chmod 0700 "$STATE_DIR" "$BACKUP_DIR"
: > "$MANIFEST.tmp"
: > "$DIRS_CREATED.tmp"

if [[ -e "$CONFIG" || -L "$CONFIG" ]]; then
  [[ -f "$CONFIG" && ! -L "$CONFIG" ]] || {
    echo "OpenClaw config is not a regular file: openclaw.json" >&2
    exit 1
  }
  cp -p "$CONFIG" "$BACKUP_DIR/openclaw.json"
  sha256sum "$CONFIG" | awk '{print $1}' > "$STATE_DIR/config-before.sha256"
else
  : > "$STATE_DIR/config-absent"
fi

for entry in "${FILES[@]}"; do
  IFS='|' read -r src_rel dest_rel mode <<< "$entry"
  src="$MODULE_DIR/$src_rel"
  dest="$TARGET/$dest_rel"
  dest_parent="$(dirname "$dest_rel")"
  [[ -f "$src" ]] || { echo "module source missing: $src_rel" >&2; exit 1; }
  record_created_parent_dirs "$dest_parent"
  existed=0
  prior_mode="-"
  if [[ -e "$dest" ]]; then
    [[ -f "$dest" && ! -L "$dest" ]] || { echo "install target is not a regular file: $dest_rel" >&2; exit 1; }
    existed=1
    prior_mode="$(stat -c '%a' "$dest")"
    mkdir -p "$BACKUP_DIR/$dest_parent"
    cp -p "$dest" "$BACKUP_DIR/$dest_rel"
  fi
  mkdir -p "$(dirname "$dest")"
  cp -p "$src" "$dest"
  chmod "$mode" "$dest"
  installed_sha="$(sha256sum "$dest" | awk '{print $1}')"
  printf '%s\t%s\t%s\t%s\t%s\n' "$dest_rel" "$existed" "$mode" "$installed_sha" "$prior_mode" >> "$MANIFEST.tmp"
done

chmod 0600 "$MANIFEST.tmp"
mv "$MANIFEST.tmp" "$MANIFEST"
sort -u "$DIRS_CREATED.tmp" > "$DIRS_CREATED"
rm -f "$DIRS_CREATED.tmp"
echo "workspace-lane-guard files installed; openclawVersion=$EXPECTED_OPENCLAW_VERSION; runtime configuration and activation remain explicit operator steps"
