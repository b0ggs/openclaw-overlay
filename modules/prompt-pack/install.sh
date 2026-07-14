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
STATE_DIR="$TARGET/.openclaw-overlay/modules/prompt-pack"
BACKUP_DIR="$STATE_DIR/backup"
MANIFEST="$STATE_DIR/manifest.tsv"
DIRS_CREATED="$STATE_DIR/dirs-created.txt"

OPENCLAW_WORKSPACE_ROOT="${OPENCLAW_WORKSPACE_ROOT:-$TARGET}"
OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
OPENCLAW_MDS_ROOT="${OPENCLAW_MDS_ROOT:-$OPENCLAW_HOME/repos/openclaw-mds}"
OPENCLAW_PROJECTS_ROOT="${OPENCLAW_PROJECTS_ROOT:-$OPENCLAW_HOME/projects}"
OPENCLAW_EXTERNAL_PROJECTS_ROOT="${OPENCLAW_EXTERNAL_PROJECTS_ROOT:-$HOME/projects}"
OPENCLAW_WORKTREES_ROOT="${OPENCLAW_WORKTREES_ROOT:-$OPENCLAW_HOME/worktrees}"
OPENCLAW_STOP_FILE="${OPENCLAW_STOP_FILE:-$OPENCLAW_HOME/STOP}"
OPENCLAW_RUNS_ROOT="${OPENCLAW_RUNS_ROOT:-$HOME/tmp/openclaw-runs}"
export OPENCLAW_HOME OPENCLAW_WORKSPACE_ROOT OPENCLAW_MDS_ROOT OPENCLAW_PROJECTS_ROOT
export OPENCLAW_EXTERNAL_PROJECTS_ROOT OPENCLAW_WORKTREES_ROOT OPENCLAW_STOP_FILE OPENCLAW_RUNS_ROOT

FILES=(
  "BOOT.md|BOOT.md|0644"
  "ORCHESTRATOR.md|ORCHESTRATOR.md|0644"
  "CODER.md|CODER.md|0644"
  "ANALYST.md|ANALYST.md|0644"
  "RESEARCHER.md|RESEARCHER.md|0644"
  "PIPELINER.md|PIPELINER.md|0644"
  "AUDITOR_ALPHA.md|AUDITOR_ALPHA.md|0644"
  "AUDITOR_ALPHA_PRIME.md|AUDITOR_ALPHA_PRIME.md|0644"
  "AUDITOR_BETA.md|AUDITOR_BETA.md|0644"
  "MEDIATOR.md|MEDIATOR.md|0644"
  "REVIEWER_DATA.md|REVIEWER_DATA.md|0644"
  "REVIEWER_OPS.md|REVIEWER_OPS.md|0644"
  "SUPERVISOR.md|SUPERVISOR.md|0644"
  "SKILLS_ROUTING.md|SKILLS_ROUTING.md|0644"
  "docs/on-demand/BOOT.details.md|docs/on-demand/BOOT.details.md|0644"
  "docs/on-demand/ORCHESTRATOR.full.md|docs/on-demand/ORCHESTRATOR.full.md|0644"
  "docs/on-demand/WORKFLOW.full.md|docs/on-demand/WORKFLOW.full.md|0644"
  "docs/prompt-pack/path-placeholders.md|docs/prompt-pack/path-placeholders.md|0644"
  "schemas/orchestrator/research-phase-state.schema.json|schemas/orchestrator/research-phase-state.schema.json|0644"
  "schemas/reviews/auditor-logic-review.schema.json|schemas/reviews/auditor-logic-review.schema.json|0644"
  "schemas/reviews/auditor-security-review.schema.json|schemas/reviews/auditor-security-review.schema.json|0644"
  "schemas/reviews/mediator-final-verdict.schema.json|schemas/reviews/mediator-final-verdict.schema.json|0644"
  "schemas/reviews/reviewer-summary.schema.json|schemas/reviews/reviewer-summary.schema.json|0644"
  "scripts/render-boot-index.py|scripts/render-boot-index.py|0755"
)

render_file() {
  local src="$1"
  local dest="$2"
  python3 - "$src" "$dest" <<'PY'
from __future__ import annotations

import os
import sys
from pathlib import Path

src = Path(sys.argv[1])
dest = Path(sys.argv[2])
text = src.read_text(encoding="utf-8")
for key in (
    "OPENCLAW_HOME",
    "OPENCLAW_WORKSPACE_ROOT",
    "OPENCLAW_MDS_ROOT",
    "OPENCLAW_PROJECTS_ROOT",
    "OPENCLAW_EXTERNAL_PROJECTS_ROOT",
    "OPENCLAW_WORKTREES_ROOT",
    "OPENCLAW_STOP_FILE",
    "OPENCLAW_RUNS_ROOT",
):
    text = text.replace("${" + key + "}", os.environ[key])
dest.write_text(text, encoding="utf-8")
PY
}

render_source() {
  local src_rel="$1"
  local out="$2"
  local src="$SRC_DIR/$src_rel"
  if [[ ! -f "$src" ]]; then
    echo "module source missing: $src_rel" >&2
    exit 1
  fi
  render_file "$src" "$out"
}

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
    tmp="$(mktemp)"
    render_source "$src_rel" "$tmp"
    if [[ ! -f "$TARGET/$dest_rel" ]]; then
      rm -f "$tmp"
      echo "installed file missing: $dest_rel" >&2
      exit 1
    fi
    if ! cmp -s "$tmp" "$TARGET/$dest_rel"; then
      rm -f "$tmp"
      echo "installed file differs from rendered module source: $dest_rel" >&2
      exit 1
    fi
    chmod "$mode" "$TARGET/$dest_rel"
    rm -f "$tmp"
  done
  exit 0
fi

mkdir -p "$BACKUP_DIR"
: > "$MANIFEST.tmp"
: > "$DIRS_CREATED.tmp"

for entry in "${FILES[@]}"; do
  IFS='|' read -r src_rel dest_rel mode <<< "$entry"
  dest="$TARGET/$dest_rel"
  dest_parent="$(dirname "$dest_rel")"
  tmp="$(mktemp)"
  render_source "$src_rel" "$tmp"
  installed_sha="$(sha256sum "$tmp" | awk '{print $1}')"

  record_created_parent_dirs "$dest_parent"

  existed=0
  if [[ -e "$dest" ]]; then
    existed=1
    mkdir -p "$BACKUP_DIR/$dest_parent"
    cp -p "$dest" "$BACKUP_DIR/$dest_rel"
  fi

  mkdir -p "$(dirname "$dest")"
  cp "$tmp" "$dest"
  chmod "$mode" "$dest"
  rm -f "$tmp"
  printf '%s\t%s\t%s\t%s\n' "$dest_rel" "$existed" "$mode" "$installed_sha" >> "$MANIFEST.tmp"
done

mv "$MANIFEST.tmp" "$MANIFEST"
sort -u "$DIRS_CREATED.tmp" > "$DIRS_CREATED"
rm -f "$DIRS_CREATED.tmp"
