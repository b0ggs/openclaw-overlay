#!/usr/bin/env python3
"""Metadata-only restore/change-scope classifier for finalizer gating.

This helper classifies repository-relative paths only.  It does not inspect raw
target-file contents, diffs, environment variables, credentials, runtime state,
or live OpenClaw config.  The output is intentionally limited to path metadata
and a boolean decision about whether the restore-completeness checker is
required.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

SCHEMA_VERSION = 1

ROOT_WORKFLOW_DOCS = frozenset(
    {
        "AGENTS.md",
        "BOOT.md",
        "BOOTSTRAP.md",
        "CODEX.md",
        "DREAMS.md",
        "HEARTBEAT.md",
        "IDENTITY.md",
        "NEEDS.md",
        "ORCHESTRATOR.md",
        "PATTERNS.md",
        "PROJECT_INIT.md",
        "README.md",
        "SKILLS_ROUTING.md",
        "SOUL.md",
        "STATE.md",
        "SUPERVISOR.md",
        "TOOLS.md",
        "USER.md",
        "WORKFLOW.md",
    }
)

ROLE_WORKFLOW_DOCS = frozenset(
    {
        "ANALYST.md",
        "AUDITOR_ALPHA.md",
        "AUDITOR_ALPHA_PRIME.md",
        "AUDITOR_BETA.md",
        "CODER.md",
        "MEDIATOR.md",
        "PIPELINER.md",
        "RESEARCHER.md",
        "REVIEWER_DATA.md",
        "REVIEWER_OPS.md",
    }
)

# First match wins.  Keep status/evidence and restore documentation before the
# broad docs-safe rule below.
HARNESS_AFFECTING_PATTERNS: tuple[tuple[str, str], ...] = (
    (".github/workflows/**", "github_workflow"),
    ("scripts/**", "script"),
    ("harness/**", "harness_core"),
    ("templates/**", "template"),
    ("prompts/**", "prompt"),
    ("docs/restore/**", "restore_doc"),
    ("status/**", "status_metadata"),
    ("docs/status/**", "status_metadata"),
    ("reports/status/**", "status_metadata"),
    ("artifacts/status/**", "status_metadata"),
    ("evidence/**", "evidence_metadata"),
    ("docs/evidence/**", "evidence_metadata"),
    ("reports/evidence/**", "evidence_metadata"),
    ("artifacts/evidence/**", "evidence_metadata"),
    ("checks/**", "check_metadata"),
    ("docs/mds/**", "mds_ci_check"),
    ("mds/**", "mds_ci_check"),
)

STATUS_EVIDENCE_BASENAME_PATTERNS: tuple[tuple[str, str], ...] = (
    ("status.json", "status_metadata"),
    ("*.status.json", "status_metadata"),
    ("*-status.json", "status_metadata"),
    ("evidence.json", "evidence_metadata"),
    ("*.evidence.json", "evidence_metadata"),
    ("*-evidence.json", "evidence_metadata"),
    ("restore-evidence.json", "evidence_metadata"),
    ("restore-status.json", "status_metadata"),
)

MDS_CI_CHECK_BASENAME_PATTERNS: tuple[str, ...] = (
    "mds-ci.yml",
    "mds-ci.yaml",
    "mds-check.yml",
    "mds-check.yaml",
    "*mds*check*.yml",
    "*mds*check*.yaml",
    "*mds*ci*.yml",
    "*mds*ci*.yaml",
)


def normalize_scope_path(raw_path: str) -> str:
    """Normalize a path string to a safe repo-relative POSIX-ish form."""

    if not isinstance(raw_path, str):
        raise ValueError("path must be a string")
    path = raw_path.strip().replace("\\", "/")
    if not path or "\0" in path:
        raise ValueError("path must be non-empty and must not contain NUL")
    if path.startswith("/"):
        raise ValueError("path must be repository-relative")
    while path.startswith("./"):
        path = path[2:]
    parts = PurePosixPath(path).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("path must be a normalized repository-relative path without traversal segments")
    normalized = PurePosixPath(*parts).as_posix()
    if normalized in {"", "."}:
        raise ValueError("path must stay within the repository")
    return normalized


def _fnmatch(path: str, pattern: str) -> bool:
    # fnmatchcase keeps POSIX metadata classification stable and lets ** cover
    # nested repository-relative paths without reading file contents.
    return fnmatch.fnmatchcase(path, pattern)


def classify_path(path: str) -> dict[str, Any]:
    """Return metadata-only restore-scope classification for one path."""

    normalized = normalize_scope_path(path)
    basename = normalized.rsplit("/", 1)[-1]

    if normalized in ROOT_WORKFLOW_DOCS:
        return {
            "path": normalized,
            "classification": "root_workflow_doc",
            "restoreRequired": True,
            "reasonCode": "root_workflow_doc",
        }

    if normalized in ROLE_WORKFLOW_DOCS:
        return {
            "path": normalized,
            "classification": "role_workflow_doc",
            "restoreRequired": True,
            "reasonCode": "role_workflow_doc",
        }

    for pattern, reason_code in HARNESS_AFFECTING_PATTERNS:
        if _fnmatch(normalized, pattern):
            return {
                "path": normalized,
                "classification": reason_code,
                "restoreRequired": True,
                "reasonCode": reason_code,
            }

    for pattern, reason_code in STATUS_EVIDENCE_BASENAME_PATTERNS:
        if _fnmatch(basename, pattern):
            return {
                "path": normalized,
                "classification": reason_code,
                "restoreRequired": True,
                "reasonCode": reason_code,
            }

    for pattern in MDS_CI_CHECK_BASENAME_PATTERNS:
        if _fnmatch(basename, pattern):
            return {
                "path": normalized,
                "classification": "mds_ci_check",
                "restoreRequired": True,
                "reasonCode": "mds_ci_check",
            }

    if normalized.startswith("docs/"):
        return {
            "path": normalized,
            "classification": "docs_safe",
            "restoreRequired": False,
            "reasonCode": "docs_safe",
        }

    return {
        "path": normalized,
        "classification": "ordinary_path",
        "restoreRequired": False,
        "reasonCode": "ordinary_path",
    }


def _source_map(*, changed_paths: Iterable[str], manifest_paths: Iterable[str]) -> dict[str, set[str]]:
    sources: dict[str, set[str]] = {}
    for source, paths in (("changed", changed_paths), ("manifest", manifest_paths)):
        for raw_path in paths:
            path = normalize_scope_path(raw_path)
            sources.setdefault(path, set()).add(source)
    return sources


def build_scope(*, changed_paths: Iterable[str] = (), manifest_paths: Iterable[str] = ()) -> dict[str, Any]:
    """Classify changed and manifest paths and decide if restore evidence is required."""

    path_sources = _source_map(changed_paths=changed_paths, manifest_paths=manifest_paths)
    entries: list[dict[str, Any]] = []
    for path in sorted(path_sources):
        entry = classify_path(path)
        entry["sources"] = sorted(path_sources[path])
        entries.append(entry)

    harness_paths = [entry["path"] for entry in entries if entry["restoreRequired"]]
    safe_paths = [entry["path"] for entry in entries if not entry["restoreRequired"]]
    reason_codes = sorted({entry["reasonCode"] for entry in entries if entry["restoreRequired"]})

    return {
        "schemaVersion": SCHEMA_VERSION,
        "restoreRequired": bool(harness_paths),
        "required": bool(harness_paths),
        "reasonCodes": reason_codes,
        "harnessAffectingPaths": harness_paths,
        "changedHarnessAffectingPaths": [
            entry["path"] for entry in entries if entry["restoreRequired"] and "changed" in entry["sources"]
        ],
        "manifestHarnessAffectingPaths": [
            entry["path"] for entry in entries if entry["restoreRequired"] and "manifest" in entry["sources"]
        ],
        "safePaths": safe_paths,
        "paths": entries,
        "policy": {
            "metadataOnly": True,
            "rawDiffOutput": False,
            "rawLogOutput": False,
            "liveRuntimeMutation": False,
        },
    }


def _split_nul(data: str) -> list[str]:
    return [entry for entry in data.split("\0") if entry]


def collect_changed_paths(repo: str) -> list[str]:
    """Collect changed paths from git without reading diffs or file contents."""

    paths: set[str] = set()
    for args in (
        ["diff", "--name-only", "-z", "--"],
        ["diff", "--cached", "--name-only", "-z", "--"],
        ["ls-files", "--others", "--exclude-standard", "-z"],
    ):
        completed = subprocess.run(
            ["git", "-C", repo, *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("git path collection failed")
        paths.update(_split_nul(completed.stdout))
    return sorted(paths)


def _repo_local_manifest_file(repo: str, path: str) -> Path:
    repo_root = Path(repo).expanduser().resolve()
    rel_path = normalize_scope_path(path)
    manifest = (repo_root / rel_path).resolve(strict=False)
    try:
        manifest.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError("manifest file must stay within the repository") from exc
    if not manifest.is_file():
        raise ValueError("manifest file must be a repo-local readable file")
    return manifest


def _load_manifest_file(repo: str, path: str) -> list[str]:
    text = _repo_local_manifest_file(repo, path).read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        data = json.loads(text)
        if isinstance(data, list) and all(isinstance(item, str) for item in data):
            return list(data)
        if isinstance(data, dict):
            paths = data.get("paths", data.get("manifestPaths"))
            if isinstance(paths, list) and all(isinstance(item, str) for item in paths):
                return list(paths)
        raise ValueError("manifest JSON must be a list or object with string paths")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Classify changed paths for restore/change-scope gating")
    parser.add_argument("--repo", default=".", help="repository path, used only when collecting changed paths")
    parser.add_argument("--path", action="append", default=[], help="path to classify as both changed and manifest")
    parser.add_argument("--changed-path", action="append", default=[], help="changed path to classify")
    parser.add_argument("--manifest-path", action="append", default=[], help="manifest path to classify")
    parser.add_argument("--manifest-file", action="append", default=[], help="JSON or newline manifest file of paths")
    parser.add_argument("--collect-git", action="store_true", help="collect changed paths from git metadata")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    changed_paths = list(args.changed_path) + list(args.path)
    manifest_paths = list(args.manifest_path) + list(args.path)
    for manifest_file in args.manifest_file:
        manifest_paths.extend(_load_manifest_file(args.repo, manifest_file))
    if args.collect_git:
        changed_paths.extend(collect_changed_paths(args.repo))
    try:
        payload = build_scope(changed_paths=changed_paths, manifest_paths=manifest_paths)
    except (RuntimeError, ValueError) as exc:
        payload = {"schemaVersion": SCHEMA_VERSION, "ok": False, "error": {"kind": "scope_check_failed", "message": str(exc)}}
        print(json.dumps(payload, sort_keys=True))
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
