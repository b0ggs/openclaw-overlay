#!/usr/bin/env bash
set -euo pipefail

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SRC_DIR="$MODULE_DIR/src"
TARGET="${1:-$PWD}"

if [[ ! -d "$TARGET" ]]; then
  echo "target workspace does not exist: $TARGET" >&2
  exit 2
fi

TARGET="$(cd "$TARGET" && pwd -P)"
STATE_DIR="$TARGET/.openclaw-overlay/modules/policy-pack"
BACKUP_DIR="$STATE_DIR/backup"
MANIFEST="$STATE_DIR/manifest.tsv"
DIRS_CREATED="$STATE_DIR/dirs-created.txt"

FILES=(
  "../../../core/harness/path_config.py|harness/path_config.py|0644"
  "AGENTS.md|AGENTS.md|0644"
  "WORKFLOW.md|WORKFLOW.md|0644"
  "scripts/git-preflight.sh|scripts/git-preflight.sh|0755"
  "scripts/openclaw-runtime-preflight.py|scripts/openclaw-runtime-preflight.py|0755"
)

if [[ -f "$MANIFEST" ]]; then
  for entry in "${FILES[@]}"; do
    IFS='|' read -r src_rel dest_rel mode <<< "$entry"
    src="$SRC_DIR/$src_rel"
    if [[ ! -f "$TARGET/$dest_rel" ]]; then
      echo "installed file missing: $dest_rel" >&2
      exit 1
    fi
    if ! cmp -s "$src" "$TARGET/$dest_rel"; then
      echo "installed file differs from module source: $dest_rel" >&2
      exit 1
    fi
    chmod "$mode" "$TARGET/$dest_rel"
  done
  exit 0
fi

mkdir -p "$BACKUP_DIR"
: > "$MANIFEST.tmp"
: > "$DIRS_CREATED.tmp"

for entry in "${FILES[@]}"; do
  IFS='|' read -r src_rel dest_rel mode <<< "$entry"
  src="$SRC_DIR/$src_rel"
  dest="$TARGET/$dest_rel"
  dest_parent="$(dirname "$dest_rel")"

  if [[ ! -f "$src" ]]; then
    echo "module source missing: $src_rel" >&2
    exit 1
  fi

  if [[ "$dest_parent" != "." && ! -d "$TARGET/$dest_parent" ]]; then
    echo "$dest_parent" >> "$DIRS_CREATED.tmp"
  fi

  existed=0
  if [[ -e "$dest" ]]; then
    existed=1
    mkdir -p "$BACKUP_DIR/$dest_parent"
    cp -p "$dest" "$BACKUP_DIR/$dest_rel"
  fi

  mkdir -p "$(dirname "$dest")"
  cp -p "$src" "$dest"
  chmod "$mode" "$dest"
  printf '%s\t%s\t%s\n' "$dest_rel" "$existed" "$mode" >> "$MANIFEST.tmp"
done

mv "$MANIFEST.tmp" "$MANIFEST"
sort -u "$DIRS_CREATED.tmp" > "$DIRS_CREATED"
rm -f "$DIRS_CREATED.tmp"
