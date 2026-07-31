#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:-$PWD}"
if [[ ! -d "$TARGET" ]]; then
  echo "target OpenClaw state directory does not exist: $TARGET" >&2
  exit 2
fi
TARGET="$(cd "$TARGET" && pwd -P)"
STATE_DIR="$TARGET/.openclaw-overlay/modules/workspace-lane-guard"
BACKUP_DIR="$STATE_DIR/backup"
MANIFEST="$STATE_DIR/manifest.tsv"
DIRS_CREATED="$STATE_DIR/dirs-created.txt"
CONFIG="$TARGET/openclaw.json"

[[ -f "$MANIFEST" ]] || exit 0

# A separately authorized scheduler creator records the supported Cron id here.
# Removal is fail-closed; no internal scheduler storage is read or edited.
if [[ -s "$STATE_DIR/monitor-cron-id" ]]; then
  cron_id="$(tr -d '\\r\\n' < "$STATE_DIR/monitor-cron-id")"
  [[ "$cron_id" =~ ^[A-Za-z0-9_-]{1,128}$ ]] || { echo "invalid recorded monitor cron id" >&2; exit 1; }
  OPENCLAW_STATE_DIR="$TARGET" openclaw cron rm "$cron_id"
fi

while IFS=$'\t' read -r dest_rel existed mode installed_sha prior_mode; do
  [[ -z "${dest_rel:-}" ]] && continue
  dest="$TARGET/$dest_rel"
  if [[ -e "$dest" ]]; then
    [[ -f "$dest" && ! -L "$dest" ]] || { echo "installed path changed type, refusing uninstall: $dest_rel" >&2; exit 1; }
    current_sha="$(sha256sum "$dest" | awk '{print $1}')"
    [[ "$current_sha" == "$installed_sha" ]] || {
      echo "target file changed after install, refusing uninstall: $dest_rel" >&2
      exit 1
    }
  fi
  if [[ "$existed" == "1" ]]; then
    [[ -f "$BACKUP_DIR/$dest_rel" ]] || { echo "backup missing for: $dest_rel" >&2; exit 1; }
    [[ "${prior_mode:-}" =~ ^[0-7]{3,4}$ ]] || { echo "invalid backup mode for: $dest_rel" >&2; exit 1; }
    mkdir -p "$(dirname "$dest")"
    cp -p "$BACKUP_DIR/$dest_rel" "$dest"
    chmod "$prior_mode" "$dest"
  else
    rm -f "$dest"
  fi
done < "$MANIFEST"

if [[ -f "$STATE_DIR/config-before.sha256" ]]; then
  [[ -f "$BACKUP_DIR/openclaw.json" ]] || { echo "OpenClaw config backup missing" >&2; exit 1; }
  expected_config_sha="$(tr -d '\r\n' < "$STATE_DIR/config-before.sha256")"
  backup_config_sha="$(sha256sum "$BACKUP_DIR/openclaw.json" | awk '{print $1}')"
  [[ "$backup_config_sha" == "$expected_config_sha" ]] || { echo "OpenClaw config backup checksum mismatch" >&2; exit 1; }
  if [[ -e "$CONFIG" || -L "$CONFIG" ]]; then
    [[ -f "$CONFIG" && ! -L "$CONFIG" ]] || { echo "OpenClaw config changed type, refusing uninstall" >&2; exit 1; }
  fi
  cp -p "$BACKUP_DIR/openclaw.json" "$CONFIG"
elif [[ -f "$STATE_DIR/config-absent" ]]; then
  if [[ -e "$CONFIG" || -L "$CONFIG" ]]; then
    [[ -f "$CONFIG" && ! -L "$CONFIG" ]] || { echo "OpenClaw config changed type, refusing uninstall" >&2; exit 1; }
    rm -f "$CONFIG"
  fi
else
  echo "OpenClaw config backup metadata missing" >&2
  exit 1
fi

if [[ -f "$DIRS_CREATED" ]]; then
  while IFS= read -r rel_dir; do
    [[ -z "$rel_dir" ]] && continue
    rmdir "$TARGET/$rel_dir" 2>/dev/null || true
  done < <(awk '{ print length, $0 }' "$DIRS_CREATED" | sort -rn | cut -d' ' -f2-)
fi

rm -rf "$STATE_DIR"
rmdir "$TARGET/.openclaw-overlay/modules" 2>/dev/null || true
rmdir "$TARGET/.openclaw-overlay" 2>/dev/null || true
