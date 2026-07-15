#!/usr/bin/env python3
"""Exact-path finalizer manifest helper.

Wave 2 is intentionally conservative: dry-run is the default and emits only
metadata about the manifest and dirty worktree paths.  The optional staging mode
stages the exact validated manifest paths and never commits or pushes.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
PREPUSH_SECRET_SCAN_SCRIPT = "scripts/prepush-secret-scan.py"
RESTORE_COMPLETENESS_CHECK_SCRIPT = "scripts/restore-completeness-check.py"
RESTORE_CHANGE_SCOPE_CHECK_SCRIPT = "restore-change-scope-check.py"
RESTORE_GATE_SCHEMA_VERSION = 1
RESTORE_EVIDENCE_PASS_STATUSES = {"pass", "passed", "ok", "success"}
RESTORE_EVIDENCE_FAIL_STATUSES = {"fail", "failed", "failure", "blocked", "error"}
COMMIT_OID_RE = re.compile(r"^[0-9a-f]{40}$")
FORBIDDEN_RESTORE_EVIDENCE_EXACT_KEYS = {"env"}
FORBIDDEN_RESTORE_EVIDENCE_KEY_FRAGMENTS = (
    "apikey",
    "auth",
    "authorization",
    "credential",
    "diff",
    "environment",
    "envvar",
    "log",
    "output",
    "password",
    "patch",
    "private",
    "privatekey",
    "raw",
    "secret",
    "session",
    "stderr",
    "stdout",
    "token",
)


class FinalizerError(Exception):
    """Structured finalizer failure that is safe to serialize."""

    def __init__(self, kind: str, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.path = path

    def to_dict(self) -> dict[str, str]:
        payload = {"kind": self.kind, "message": self.message}
        if self.path is not None:
            payload["path"] = self.path
        return payload


def run_git(repo_root: Path, args: Sequence[str]) -> str:
    """Run git and return stdout without exposing stdout/stderr on failures."""

    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        label = " ".join(args[:2]) if len(args) >= 2 else " ".join(args)
        raise FinalizerError("git_command_failed", f"git command failed: {label}")
    return proc.stdout


def split_nul(data: str) -> list[str]:
    return [entry for entry in data.split("\0") if entry]


def unique_sorted(paths: Iterable[str]) -> list[str]:
    return sorted(set(paths))


def resolve_repo_root(repo_path: str) -> Path:
    candidate = Path(repo_path).expanduser().resolve()
    output = run_git(candidate, ["rev-parse", "--show-toplevel"])
    root = output.strip()
    if not root:
        raise FinalizerError("repo_not_found", "unable to resolve repository root")
    return Path(root).resolve()


def verify_base_commit(repo_root: Path, base_commit: str) -> str:
    if not base_commit or base_commit.startswith("-"):
        raise FinalizerError("invalid_base_commit", "base commit is required")
    output = run_git(repo_root, ["rev-parse", "--verify", f"{base_commit}^{{commit}}"])
    oid = output.strip()
    if not oid:
        raise FinalizerError("invalid_base_commit", "base commit did not resolve to a commit")
    return oid


def current_head_commit(repo_root: Path) -> str:
    output = run_git(repo_root, ["rev-parse", "--verify", "HEAD^{commit}"])
    oid = output.strip()
    if not oid:
        raise FinalizerError("invalid_head_commit", "HEAD did not resolve to a commit")
    return oid


def validate_destination_ref(destination_ref: str) -> None:
    if not destination_ref or destination_ref.startswith("-") or "\0" in destination_ref:
        raise FinalizerError("invalid_destination_ref", "destination ref is required")


def prepush_secret_scan_metadata(repo_root: Path, base_commit_oid: str) -> dict[str, Any]:
    """Record the Wave 3 scanner requirement without invoking it implicitly."""

    range_spec = f"{base_commit_oid}..HEAD"
    script_path = repo_root / PREPUSH_SECRET_SCAN_SCRIPT
    return {
        "required": True,
        "invoked": False,
        "status": "available_required" if script_path.is_file() else "missing_required",
        "script": PREPUSH_SECRET_SCAN_SCRIPT,
        "scopes": ["staged", "range"],
        "range": range_spec,
        "command": [
            sys.executable,
            PREPUSH_SECRET_SCAN_SCRIPT,
            "--repo",
            str(repo_root),
            "--range",
            range_spec,
            "--json",
        ],
        "policy": {
            "output": "metadata_only_redacted",
            "rawDiffOutput": False,
            "binaryLargeUnreadable": "fail_closed",
        },
    }


def observed_remote_url(repo_root: Path) -> str:
    try:
        value = run_git(repo_root, ["remote", "get-url", "origin"]).strip()
    except FinalizerError:
        return ""
    return value


def _repo_name_from_remote_path(path: str) -> tuple[str | None, str | None]:
    parts = [part for part in path.strip().lstrip("/").split("/") if part]
    if len(parts) < 2:
        return None, None
    repo = parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return parts[0] or None, repo or None


def safe_remote_metadata(remote_value: str) -> dict[str, Any]:
    """Return origin-remote shape without retaining any embedded credentials."""

    metadata: dict[str, Any] = {
        "metadataOnly": True,
        "valueExposure": False,
        "present": bool(remote_value.strip()),
        "protocol": None,
        "host": None,
        "owner": None,
        "repo": None,
        "userinfoPresent": False,
        "parseable": False,
    }
    value = remote_value.strip()
    if not value:
        return metadata

    parsed = urlsplit(value)
    if parsed.scheme and parsed.netloc:
        metadata["protocol"] = parsed.scheme.lower()
        metadata["host"] = parsed.hostname.lower() if parsed.hostname else None
        metadata["userinfoPresent"] = bool(parsed.username or parsed.password or ("@" in parsed.netloc and parsed.hostname))
        owner, repo = _repo_name_from_remote_path(parsed.path)
        metadata["owner"] = owner
        metadata["repo"] = repo
        metadata["parseable"] = bool(metadata["host"] and owner and repo)
        return metadata

    scp_like = re.match(r"^(?:(?P<user>[^@/:]+)@)?(?P<host>[^:/]+):(?P<path>.+)$", value)
    if scp_like:
        metadata["protocol"] = "ssh"
        metadata["host"] = scp_like.group("host").lower()
        metadata["userinfoPresent"] = False
        owner, repo = _repo_name_from_remote_path(scp_like.group("path"))
        metadata["owner"] = owner
        metadata["repo"] = repo
        metadata["parseable"] = bool(metadata["host"] and owner and repo)
        return metadata

    metadata["protocol"] = "unparseable"
    return metadata


def non_secret_credential_lane_metadata(repo_root: Path) -> dict[str, Any]:
    """Return credential-lane shape only; never token values or helper output."""

    helper = ""
    try:
        helper = run_git(repo_root, ["config", "--get", "credential.helper"]).strip()
    except FinalizerError:
        helper = ""
    return {
        "metadataOnly": True,
        "valueExposure": False,
        "allowedLane": "repo-authority-policy-or-human-approved-lane",
        "observedHelperConfigured": bool(helper),
        "observedHelperType": "configured" if helper else "none",
        "source": "git config credential.helper presence only",
    }


def section4_report_metadata(
    *,
    repo_root: Path,
    issue_id: str,
    destination_ref: str,
    subject_commit_oid: str,
    manifest_paths: Sequence[str],
    changed_paths: Sequence[str],
    unrelated_dirty_paths: Sequence[str],
    would_stage_paths: Sequence[str],
    base_commit_oid: str,
    restore_gate: dict[str, Any],
) -> dict[str, Any]:
    remote = safe_remote_metadata(observed_remote_url(repo_root))
    return {
        "schemaVersion": 1,
        "metadataOnly": True,
        "redaction": {
            "sensitiveValues": "excluded",
            "consoleText": "excluded",
            "changeContent": "excluded",
        },
        "issueId": issue_id,
        "repo": {
            "name": repo_root.name,
            "localRoot": str(repo_root),
            "ownerExpectation": "repo-authority-policy",
            "visibilityExpectation": "repo-authority-policy",
            "remote": remote,
        },
        "pathScope": {
            "approvedPaths": list(manifest_paths),
            "changedPaths": list(changed_paths),
            "stagedOrCommittedPaths": [],
            "wouldStagePaths": list(would_stage_paths),
            "outOfScopePaths": list(unrelated_dirty_paths),
        },
        "destination": {
            "destinationRef": destination_ref,
            "commitSha": subject_commit_oid,
            "baseCommitSha": base_commit_oid,
            "defaultRefUpdated": False,
            "defaultBranchName": None,
            "localRemoteRelation": "not_verified_by_manifest_helper",
        },
        "branchPrState": {
            "branchName": None,
            "pullRequest": None,
            "merged": False,
            "cleanupDeleteStatus": "not_performed",
            "lifetimeOwnerDeadline": "not_recorded_by_manifest_helper",
        },
        "dirtyState": {
            "dirtyFilesRemaining": list(changed_paths),
            "unrelatedDirtyPaths": list(unrelated_dirty_paths),
            "classification": "manifest_scope_only",
        },
        "checks": {
            "preflightSafetyScan": "metadata_recorded_not_invoked",
            "restoreGateStatus": restore_gate.get("status"),
            "requiredCheckMapping": [],
        },
        "accessLane": non_secret_credential_lane_metadata(repo_root),
        "repoIdentityEvidence": {
            "barredAccountRejected": "not_verified_by_manifest_helper",
            "officialUpstreamNotTarget": "not_verified_by_manifest_helper",
        },
        "humanOnlyOperations": {
            "required": ["publication_or_default_ref_landing_if_needed"],
            "performedByHuman": [],
            "notPerformed": ["publication_or_default_ref_landing"],
        },
        "restoreRehearsalStatus": restore_gate.get("status") if restore_gate.get("required") else "NOT_DONE",
        "doneStatus": "NOT_DONE",
        "readyForHuman": True,
    }


def normalize_manifest_path(repo_root: Path, raw_path: str) -> str:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise FinalizerError("manifest_path_empty", "manifest path must be non-empty")
    if "\0" in raw_path:
        raise FinalizerError("manifest_path_invalid", "manifest path contains a NUL byte")

    expanded = Path(raw_path).expanduser()
    candidate = expanded if expanded.is_absolute() else repo_root / expanded
    resolved = candidate.resolve(strict=False)
    try:
        relative = resolved.relative_to(repo_root)
    except ValueError as exc:
        raise FinalizerError(
            "manifest_path_escape",
            "manifest path escapes repository",
            path=raw_path,
        ) from exc

    rel_posix = relative.as_posix()
    if rel_posix in {"", "."}:
        raise FinalizerError(
            "manifest_path_not_exact",
            "manifest path must name an exact file path, not the repository root",
            path=raw_path,
        )
    if rel_posix == ".git" or rel_posix.startswith(".git/"):
        raise FinalizerError(
            "manifest_path_git_dir",
            "manifest path must not target git internals",
            path=raw_path,
        )
    if (repo_root / rel_posix).is_dir():
        raise FinalizerError(
            "manifest_path_directory",
            "manifest path must name an exact file path, not a directory",
            path=raw_path,
        )
    return rel_posix


def resolve_manifest_file(repo_root: Path, path_arg: str) -> Path:
    rel_path = normalize_manifest_path(repo_root, path_arg)
    manifest_file = repo_root / rel_path
    if not manifest_file.is_file():
        raise FinalizerError(
            "manifest_file_unreadable",
            "manifest file must be a repo-local readable file",
            path=path_arg,
        )
    return manifest_file


def load_manifest_file(repo_root: Path, path_arg: str) -> list[str]:
    manifest_file = resolve_manifest_file(repo_root, path_arg)
    try:
        text = manifest_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise FinalizerError("manifest_file_unreadable", "unable to read manifest file", path=path_arg) from exc

    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise FinalizerError("manifest_file_invalid_json", "manifest file is not valid JSON") from exc
        if isinstance(data, list):
            paths = data
        elif isinstance(data, dict):
            paths = data.get("paths", data.get("manifestPaths"))
        else:
            paths = None
        if not isinstance(paths, list) or not all(isinstance(entry, str) for entry in paths):
            raise FinalizerError(
                "manifest_file_invalid",
                "manifest JSON must be a list or object with string paths",
            )
        return list(paths)

    paths: list[str] = []
    for line in text.splitlines():
        item = line.strip()
        if item and not item.startswith("#"):
            paths.append(item)
    return paths


def normalize_manifest_paths(repo_root: Path, raw_paths: Iterable[str]) -> list[str]:
    normalized = [normalize_manifest_path(repo_root, raw) for raw in raw_paths]
    if not normalized:
        raise FinalizerError("manifest_empty", "at least one manifest path is required")
    return unique_sorted(normalized)


def collect_changed_paths(repo_root: Path, *, base_commit: str | None = None) -> list[str]:
    """Return repo-relative branch, tracked, staged, and untracked paths."""

    paths: set[str] = set()
    git_arg_groups: list[list[str]] = []
    if base_commit:
        git_arg_groups.append(["diff", "--name-only", "-z", f"{base_commit}..HEAD", "--"])
    git_arg_groups.extend(
        [
            ["diff", "--name-only", "-z", "--"],
            ["diff", "--cached", "--name-only", "-z", "--"],
            ["ls-files", "--others", "--exclude-standard", "-z"],
        ]
    )
    for args in git_arg_groups:
        for path in split_nul(run_git(repo_root, args)):
            if path and path != ".":
                paths.add(path)
    return unique_sorted(paths)


def _path_is_tracked(repo_root: Path, path: str) -> bool:
    try:
        run_git(repo_root, ["ls-files", "--error-unmatch", "--", path])
    except FinalizerError:
        return False
    return True


def _path_is_ignored(repo_root: Path, path: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "check-ignore", "-q", "--", path],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return proc.returncode == 0


def collect_manifest_ignored_untracked_paths(repo_root: Path, manifest_paths: Sequence[str]) -> list[str]:
    """Return explicit manifest files that exist only as ignored untracked files."""

    paths: set[str] = set()
    for path in manifest_paths:
        candidate = repo_root / path
        if not candidate.exists():
            continue
        if candidate.is_dir():
            continue
        if _path_is_tracked(repo_root, path):
            continue
        if _path_is_ignored(repo_root, path):
            paths.add(path)
    return unique_sorted(paths)


def literal_pathspec(path: str) -> str:
    return f":(literal){path}"


def stage_manifest_paths(
    repo_root: Path,
    paths: Sequence[str],
    *,
    git_runner: Callable[[Path, Sequence[str]], str] = run_git,
) -> None:
    """Stage only the exact manifest paths supplied by the caller."""

    if not paths:
        return
    for path in paths:
        if not path or path == "." or path.startswith("../") or path == "..":
            raise FinalizerError("stage_path_invalid", "refusing to stage a non-exact path", path=path)
    git_runner(repo_root, ["add", "-f", "--", *[literal_pathspec(path) for path in paths]])


def _load_restore_change_scope_module() -> Any:
    helper_path = Path(__file__).resolve().with_name(RESTORE_CHANGE_SCOPE_CHECK_SCRIPT)
    if not helper_path.is_file():
        raise FinalizerError(
            "restore_change_scope_helper_missing",
            "restore/change-scope helper is missing",
            path=f"scripts/{RESTORE_CHANGE_SCOPE_CHECK_SCRIPT}",
        )
    spec = importlib.util.spec_from_file_location("restore_change_scope_check", helper_path)
    if spec is None or spec.loader is None:
        raise FinalizerError("restore_change_scope_helper_unloadable", "restore/change-scope helper is unloadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def restore_change_scope_metadata(
    *,
    changed_paths: Sequence[str],
    manifest_paths: Sequence[str],
) -> dict[str, Any]:
    module = _load_restore_change_scope_module()
    try:
        payload = module.build_scope(changed_paths=changed_paths, manifest_paths=manifest_paths)
    except Exception as exc:  # pragma: no cover - defensive wrapper for helper errors
        raise FinalizerError("restore_change_scope_failed", "restore/change-scope helper failed") from exc
    if not isinstance(payload, dict):
        raise FinalizerError("restore_change_scope_failed", "restore/change-scope helper returned invalid metadata")
    return payload


def resolve_restore_evidence_file(repo_root: Path, path_arg: str) -> tuple[str, Path]:
    rel_path = normalize_manifest_path(repo_root, path_arg)
    evidence_file = repo_root / rel_path
    if not evidence_file.is_file():
        raise FinalizerError(
            "restore_evidence_file_unreadable",
            "restore evidence file must be a repo-local readable file",
            path=path_arg,
        )
    return rel_path, evidence_file


def _normalized_evidence_key(key: str) -> str:
    return "".join(char for char in key.lower() if char.isalnum())


def _forbidden_restore_evidence_keys(value: Any) -> list[str]:
    forbidden: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = _normalized_evidence_key(str(key))
            if normalized in FORBIDDEN_RESTORE_EVIDENCE_EXACT_KEYS or any(
                fragment in normalized for fragment in FORBIDDEN_RESTORE_EVIDENCE_KEY_FRAGMENTS
            ):
                forbidden.add(str(key))
            forbidden.update(_forbidden_restore_evidence_keys(child))
    elif isinstance(value, list):
        for child in value:
            forbidden.update(_forbidden_restore_evidence_keys(child))
    return sorted(forbidden)


def _error_field_failures(value: Any, path: str = "$", *, top_level: bool = True) -> list[str]:
    failures: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if _normalized_evidence_key(str(key)) == "errors":
                if not isinstance(child, list):
                    failures.append(child_path)
                elif child:
                    failures.append(child_path)
                continue
            failures.extend(_error_field_failures(child, child_path, top_level=False))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            failures.extend(_error_field_failures(child, f"{path}[{index}]", top_level=False))
    return failures


def _valid_commit_oid(value: str) -> bool:
    return bool(COMMIT_OID_RE.fullmatch(value))


def _status_from_restore_evidence(data: dict[str, Any]) -> tuple[str, bool | None]:
    explicit_fail = False
    passed_value = data.get("passed", data.get("ok"))
    if passed_value is False:
        explicit_fail = True
    status_value = data.get("status")
    if isinstance(status_value, str):
        normalized = status_value.strip().lower()
        if normalized in RESTORE_EVIDENCE_FAIL_STATUSES:
            explicit_fail = True
    if explicit_fail:
        return "failed", False
    if passed_value is True:
        return "passed", True
    if isinstance(status_value, str):
        normalized = status_value.strip().lower()
        if normalized in RESTORE_EVIDENCE_PASS_STATUSES:
            return "passed", True
    return "invalid", None


def _string_value(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _restore_evidence_check_counts(data: dict[str, Any]) -> dict[str, int]:
    checks = data.get("checks")
    if not isinstance(checks, list):
        return {}
    failed = 0
    invalid = 0
    for check in checks:
        if not isinstance(check, dict):
            invalid += 1
            continue
        check_status, check_passed = _status_from_restore_evidence(check)
        if check_status == "invalid" or check_passed is not True or _error_field_failures(check):
            failed += 1
    return {"checkCount": len(checks), "failedCheckCount": failed, "invalidCheckCount": invalid}


def restore_evidence_summary_from_payload(
    data: Any,
    *,
    mode: str,
    subject_commit_oid: str,
    artifact_path: str | None = None,
    command: Sequence[str] | None = None,
    return_code: int | None = None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "schemaVersion": RESTORE_GATE_SCHEMA_VERSION,
        "mode": mode,
        "status": "failed",
        "passed": False,
        "subjectCommitOid": subject_commit_oid,
        "metadataOnly": True,
        "rawLogsIncluded": False,
        "rawDiffIncluded": False,
        "liveMutation": False,
        "errors": [],
    }
    if artifact_path is not None:
        summary["artifactPath"] = artifact_path
    if command is not None:
        summary["command"] = list(command)
    if return_code is not None:
        summary["returnCode"] = return_code

    if not isinstance(data, dict):
        summary["status"] = "invalid"
        summary["errors"].append({"kind": "restore_evidence_invalid", "message": "restore evidence JSON must be an object"})
        return summary

    forbidden_keys = _forbidden_restore_evidence_keys(data)
    if forbidden_keys:
        summary["status"] = "invalid"
        summary["forbiddenEvidenceKeys"] = forbidden_keys[:20]
        summary["errors"].append(
            {
                "kind": "restore_evidence_not_metadata_only",
                "message": "restore evidence contains raw-log/diff/env/credential-shaped keys",
            }
        )
        return summary

    status, passed = _status_from_restore_evidence(data)
    artifact_subject = _string_value(data, "subjectCommitOid", "subjectCommit", "headCommitOid", "headCommit")
    if mode == "artifact" and (_string_value(data, "checker", "script") is not None):
        summary["artifactCheckerProvided"] = True
    if artifact_subject is not None:
        if not _valid_commit_oid(artifact_subject):
            summary["status"] = "invalid"
            summary["errors"].append(
                {
                    "kind": "restore_evidence_invalid_subject_commit",
                    "message": "restore evidence subject commit must be a 40-character lowercase hex commit id",
                }
            )
            return summary
        summary["artifactSubjectCommitOid"] = artifact_subject
    check_counts = _restore_evidence_check_counts(data)
    summary.update(check_counts)
    error_field_failures = _error_field_failures(data)
    if error_field_failures:
        summary["status"] = "failed"
        summary["errorFieldCount"] = len(error_field_failures)
        top_level_errors = data.get("errors")
        if isinstance(top_level_errors, list):
            summary["errorCount"] = len(top_level_errors)
        summary["errors"].append(
            {
                "kind": "restore_evidence_errors_present",
                "message": "restore evidence contains non-empty or malformed errors fields",
            }
        )
        return summary
    if check_counts.get("failedCheckCount", 0) > 0 or check_counts.get("invalidCheckCount", 0) > 0:
        summary["status"] = "failed"
        summary["errors"].append(
            {
                "kind": "restore_evidence_failed_checks",
                "message": "restore evidence contains failed or invalid checks",
            }
        )
        return summary

    if artifact_subject is not None and artifact_subject != subject_commit_oid:
        summary["status"] = "failed"
        summary["errors"].append(
            {
                "kind": "restore_evidence_subject_mismatch",
                "message": "restore evidence subject commit does not match current HEAD",
            }
        )
        return summary

    if status == "passed" and passed is True and (return_code in (None, 0)):
        summary["status"] = "passed"
        summary["passed"] = True
        return summary

    if status == "invalid":
        summary["status"] = "invalid"
        summary["errors"].append(
            {
                "kind": "restore_evidence_status_missing",
                "message": "restore evidence must include a passed/ok boolean or pass/fail status",
            }
        )
    else:
        summary["status"] = "failed"
        summary["errors"].append({"kind": "restore_evidence_failed", "message": "restore evidence did not pass"})
    return summary


def load_restore_evidence_file(repo_root: Path, path_arg: str, *, subject_commit_oid: str) -> dict[str, Any]:
    rel_path, evidence_file = resolve_restore_evidence_file(repo_root, path_arg)
    try:
        data = json.loads(evidence_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return restore_evidence_summary_from_payload(
            {},
            mode="artifact",
            subject_commit_oid=subject_commit_oid,
            artifact_path=rel_path,
        ) | {
            "status": "invalid",
            "passed": False,
            "errors": [{"kind": "restore_evidence_invalid_json", "message": "restore evidence file is not valid JSON"}],
        }
    except OSError as exc:
        raise FinalizerError("restore_evidence_file_unreadable", "unable to read restore evidence file", path=path_arg) from exc
    return restore_evidence_summary_from_payload(
        data,
        mode="artifact",
        subject_commit_oid=subject_commit_oid,
        artifact_path=rel_path,
    )


def run_restore_completeness_check(
    repo_root: Path,
    *,
    subject_commit_oid: str,
    skip_git_status: bool = False,
) -> dict[str, Any]:
    checker_path = repo_root / RESTORE_COMPLETENESS_CHECK_SCRIPT
    command = [sys.executable, RESTORE_COMPLETENESS_CHECK_SCRIPT, "--repo", str(repo_root), "--json"]
    if skip_git_status:
        command.append("--skip-git-status")
    if not checker_path.is_file():
        return {
            "schemaVersion": RESTORE_GATE_SCHEMA_VERSION,
            "mode": "restore-completeness-check",
            "status": "failed",
            "passed": False,
            "subjectCommitOid": subject_commit_oid,
            "checker": RESTORE_COMPLETENESS_CHECK_SCRIPT,
            "command": command,
            "metadataOnly": True,
            "rawLogsIncluded": False,
            "rawDiffIncluded": False,
            "liveMutation": False,
            "errors": [{"kind": "restore_checker_missing", "message": "repo-local restore checker is missing"}],
        }

    proc = subprocess.run(
        command,
        cwd=str(repo_root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        data = {}
    summary = restore_evidence_summary_from_payload(
        data,
        mode="restore-completeness-check",
        subject_commit_oid=subject_commit_oid,
        command=command,
        return_code=proc.returncode,
    )
    summary["checker"] = RESTORE_COMPLETENESS_CHECK_SCRIPT
    return summary


def build_restore_gate(
    *,
    repo_root: Path,
    change_scope: dict[str, Any],
    subject_commit_oid: str,
    restore_evidence_file: str | None = None,
    run_restore_check: bool = False,
    restore_check_skip_git_status: bool = False,
) -> dict[str, Any]:
    required = bool(change_scope.get("restoreRequired"))
    gate: dict[str, Any] = {
        "schemaVersion": RESTORE_GATE_SCHEMA_VERSION,
        "required": required,
        "status": "not_required",
        "subjectCommitOid": subject_commit_oid,
        "changeScope": change_scope,
        "policy": {
            "metadataOnlyEvidence": True,
            "rawLogsIncluded": False,
            "rawDiffIncluded": False,
            "liveRuntimeMutation": False,
        },
    }

    evidence: dict[str, Any] | None = None
    if run_restore_check:
        evidence = run_restore_completeness_check(
            repo_root,
            subject_commit_oid=subject_commit_oid,
            skip_git_status=restore_check_skip_git_status,
        )
    elif restore_evidence_file:
        evidence = load_restore_evidence_file(repo_root, restore_evidence_file, subject_commit_oid=subject_commit_oid)

    if evidence is not None:
        gate["evidence"] = evidence
        gate["status"] = "passed" if evidence.get("passed") is True and evidence.get("status") == "passed" else "failed"
    elif required:
        gate["status"] = "required_missing"
        gate["errors"] = [
            {
                "kind": "restore_gate_required",
                "message": "harness-affecting paths require restore/change-scope evidence",
            }
        ]

    return gate


def restore_gate_blocks(gate: dict[str, Any]) -> bool:
    return gate.get("status") in {"required_missing", "failed", "invalid"}


def restore_gate_error(gate: dict[str, Any]) -> dict[str, str]:
    if gate.get("status") == "required_missing":
        return {"kind": "restore_gate_required", "message": "restore/change-scope gate required for harness-affecting paths"}
    return {"kind": "restore_gate_failed", "message": "restore/change-scope evidence did not pass"}


def build_result(
    *,
    repo_path: str,
    issue_id: str,
    base_commit: str,
    destination_ref: str,
    raw_manifest_paths: Sequence[str],
    mode: str,
    manifest_file_paths: Sequence[str] = (),
    restore_evidence_file: str | None = None,
    run_restore_check: bool = False,
    restore_check_skip_git_status: bool = False,
) -> dict[str, Any]:
    if not issue_id:
        raise FinalizerError("invalid_issue_id", "issue id is required")
    validate_destination_ref(destination_ref)
    repo_root = resolve_repo_root(repo_path)
    base_commit_oid = verify_base_commit(repo_root, base_commit)
    combined_manifest_paths = list(raw_manifest_paths)
    for manifest_file_path in manifest_file_paths:
        combined_manifest_paths.extend(load_manifest_file(repo_root, manifest_file_path))
    manifest_paths = normalize_manifest_paths(repo_root, combined_manifest_paths)
    changed_paths = unique_sorted(
        [
            *collect_changed_paths(repo_root, base_commit=base_commit_oid),
            *collect_manifest_ignored_untracked_paths(repo_root, manifest_paths),
        ]
    )
    subject_commit_oid = current_head_commit(repo_root)
    change_scope = restore_change_scope_metadata(changed_paths=changed_paths, manifest_paths=manifest_paths)
    restore_gate = build_restore_gate(
        repo_root=repo_root,
        change_scope=change_scope,
        subject_commit_oid=subject_commit_oid,
        restore_evidence_file=restore_evidence_file,
        run_restore_check=run_restore_check,
        restore_check_skip_git_status=restore_check_skip_git_status,
    )
    manifest_set = set(manifest_paths)
    unrelated_dirty_paths = [path for path in changed_paths if path not in manifest_set]
    would_stage_paths = [path for path in changed_paths if path in manifest_set]
    section4 = section4_report_metadata(
        repo_root=repo_root,
        issue_id=issue_id,
        destination_ref=destination_ref,
        subject_commit_oid=subject_commit_oid,
        manifest_paths=manifest_paths,
        changed_paths=changed_paths,
        unrelated_dirty_paths=unrelated_dirty_paths,
        would_stage_paths=would_stage_paths,
        base_commit_oid=base_commit_oid,
        restore_gate=restore_gate,
    )
    errors: list[dict[str, str]] = [
        {"kind": "unrelated_dirty_path", "path": path} for path in unrelated_dirty_paths
    ]
    if restore_gate_blocks(restore_gate):
        errors.append(restore_gate_error(restore_gate))

    ok = not errors
    result: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "ok": ok,
        "mode": mode,
        "dryRun": mode == "dry-run",
        "issueId": issue_id,
        "repoRoot": str(repo_root),
        "baseCommit": base_commit,
        "baseCommitOid": base_commit_oid,
        "subjectCommitOid": subject_commit_oid,
        "destinationRef": destination_ref,
        "manifestPaths": manifest_paths,
        "changedPaths": changed_paths,
        "unrelatedDirtyPaths": unrelated_dirty_paths,
        "wouldStagePaths": would_stage_paths,
        "stagedPaths": [],
        "actions": [],
        "prepushSecretScan": prepush_secret_scan_metadata(repo_root, base_commit_oid),
        "restoreGate": restore_gate,
        "section4": section4,
    }

    if errors:
        result["errors"] = errors
        if unrelated_dirty_paths:
            result["actions"].append("blocked_unrelated_dirty_state")
        if restore_gate_blocks(restore_gate):
            result["actions"].append("blocked_restore_gate")
        return result

    if restore_gate.get("status") == "passed":
        result["actions"].append("restore_gate_passed")
    else:
        result["actions"].append("restore_gate_not_required")

    if mode == "stage":
        stage_manifest_paths(repo_root, would_stage_paths)
        result["stagedPaths"] = would_stage_paths
        result["section4"]["pathScope"]["stagedOrCommittedPaths"] = would_stage_paths
        result["actions"].append("staged_exact_manifest_paths")
    else:
        result["actions"].append("dry_run_no_stage")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Exact-path finalizer manifest helper")
    parser.add_argument("--repo", "--repo-path", required=True, help="repository path")
    parser.add_argument("--issue-id", required=True, help="issue identifier")
    parser.add_argument("--base-commit", required=True, help="base commit for metadata/verification")
    parser.add_argument("--dest-ref", "--destination-ref", required=True, help="destination ref for metadata")
    parser.add_argument(
        "--path",
        "--manifest-path",
        action="append",
        default=[],
        dest="manifest_paths",
        help="repo-relative manifest path; may be repeated",
    )
    parser.add_argument(
        "--paths",
        nargs="+",
        action="append",
        default=[],
        dest="manifest_path_groups",
        help="one or more repo-relative manifest paths",
    )
    parser.add_argument(
        "--manifest-file",
        action="append",
        default=[],
        help="repo-local newline or JSON manifest file containing paths",
    )
    restore_gate = parser.add_mutually_exclusive_group()
    restore_gate.add_argument(
        "--restore-evidence-file",
        help="repo-local metadata-only JSON restore evidence artifact",
    )
    restore_gate.add_argument(
        "--run-restore-check",
        action="store_true",
        help="run repo-local scripts/restore-completeness-check.py read-only before finalizing",
    )
    parser.add_argument(
        "--restore-check-skip-git-status",
        action="store_true",
        help="when running the restore checker, skip its git-status candidate scope check",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="validate only; this is the default")
    mode.add_argument("--stage", action="store_true", help="stage exact manifest paths only")
    parser.add_argument("paths", nargs="*", help="additional manifest paths")
    return parser


def error_payload(error: FinalizerError) -> dict[str, Any]:
    return {"schemaVersion": SCHEMA_VERSION, "ok": False, "errors": [error.to_dict()]}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    raw_manifest_paths: list[str] = []
    try:
        raw_manifest_paths.extend(args.manifest_paths)
        for group in args.manifest_path_groups:
            raw_manifest_paths.extend(group)
        raw_manifest_paths.extend(args.paths)
        result = build_result(
            repo_path=args.repo,
            issue_id=args.issue_id,
            base_commit=args.base_commit,
            destination_ref=args.dest_ref,
            raw_manifest_paths=raw_manifest_paths,
            manifest_file_paths=args.manifest_file,
            restore_evidence_file=args.restore_evidence_file,
            run_restore_check=args.run_restore_check,
            restore_check_skip_git_status=args.restore_check_skip_git_status,
            mode="stage" if args.stage else "dry-run",
        )
    except FinalizerError as exc:
        print(json.dumps(error_payload(exc), sort_keys=True))
        return 1

    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
