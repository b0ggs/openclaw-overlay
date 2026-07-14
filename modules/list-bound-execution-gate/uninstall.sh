#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:-$PWD}"

if [[ ! -d "$TARGET" ]]; then
  echo "target workspace does not exist: $TARGET" >&2
  exit 2
fi

TARGET="$(cd "$TARGET" && pwd -P)"
STATE_DIR="$TARGET/.openclaw-overlay/modules/list-bound-execution-gate"
BACKUP_DIR="$STATE_DIR/backup"
MANIFEST="$STATE_DIR/manifest.tsv"
DIRS_CREATED="$STATE_DIR/dirs-created.txt"

if [[ ! -f "$MANIFEST" ]]; then
  exit 0
fi

while IFS=$'\t' read -r dest_rel existed mode installed_sha; do
  [[ -z "${dest_rel:-}" ]] && continue
  dest="$TARGET/$dest_rel"

  if [[ -e "$dest" ]]; then
    current_sha="$(sha256sum "$dest" | awk '{print $1}')"
    if [[ "$current_sha" != "$installed_sha" ]]; then
      echo "target file changed after install, refusing uninstall: $dest_rel" >&2
      exit 1
    fi
  fi

  if [[ "$existed" == "1" ]]; then
    if [[ ! -f "$BACKUP_DIR/$dest_rel" ]]; then
      echo "backup missing for: $dest_rel" >&2
      exit 1
    fi
    mkdir -p "$(dirname "$dest")"
    cp -p "$BACKUP_DIR/$dest_rel" "$dest"
    chmod "$mode" "$dest"
  else
    rm -f "$dest"
  fi
done < "$MANIFEST"

if [[ -f "$DIRS_CREATED" ]]; then
  while IFS= read -r rel_dir; do
    [[ -z "$rel_dir" ]] && continue
    rmdir "$TARGET/$rel_dir" 2>/dev/null || true
  done < <(awk '{ print length, $0 }' "$DIRS_CREATED" | sort -rn | cut -d' ' -f2-)
fi

rm -rf "$STATE_DIR"
rmdir "$TARGET/.openclaw-overlay/modules" 2>/dev/null || true
rmdir "$TARGET/.openclaw-overlay" 2>/dev/null || true
