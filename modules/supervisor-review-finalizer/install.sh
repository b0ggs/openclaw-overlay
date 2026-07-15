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
STATE_DIR="$TARGET/.openclaw-overlay/modules/supervisor-review-finalizer"
BACKUP_DIR="$STATE_DIR/backup"
MANIFEST="$STATE_DIR/manifest.tsv"
DIRS_CREATED="$STATE_DIR/dirs-created.txt"

FILES=(
  "scripts/finalizer.py|scripts/finalizer.py|0755"
  "scripts/finalizer_required.py|scripts/finalizer_required.py|0644"
  "scripts/raw_evidence_integrity.py|scripts/raw_evidence_integrity.py|0644"
  "scripts/restore-change-scope-check.py|scripts/restore-change-scope-check.py|0644"
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
    src="$SRC_DIR/$src_rel"
    if [[ ! -f "$src" ]]; then
      echo "module source missing: $src_rel" >&2
      exit 1
    fi
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

  record_created_parent_dirs "$dest_parent"

  existed=0
  if [[ -e "$dest" ]]; then
    existed=1
    mkdir -p "$BACKUP_DIR/$dest_parent"
    cp -p "$dest" "$BACKUP_DIR/$dest_rel"
  fi

  mkdir -p "$(dirname "$dest")"
  cp -p "$src" "$dest"
  chmod "$mode" "$dest"
  installed_sha="$(sha256sum "$dest" | awk '{print $1}')"
  printf '%s\t%s\t%s\t%s\n' "$dest_rel" "$existed" "$mode" "$installed_sha" >> "$MANIFEST.tmp"
done

mv "$MANIFEST.tmp" "$MANIFEST"
sort -u "$DIRS_CREATED.tmp" > "$DIRS_CREATED"
rm -f "$DIRS_CREATED.tmp"
