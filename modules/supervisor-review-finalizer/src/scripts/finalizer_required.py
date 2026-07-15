#!/usr/bin/env python3
"""Fail-closed finalizer evidence guard for issue state transitions.

This helper is intentionally metadata-only.  It validates structured
``issue.status.finalizer`` records (or a strict local-only exception) before
transitions that imply completion/review/readiness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
import sys
from pathlib import Path
from typing import Any, Iterable

try:
    from scripts import raw_evidence_integrity
except ImportError:  # pragma: no cover - direct script execution fallback
    import raw_evidence_integrity  # type: ignore

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from harness.path_config import openclaw_home

CONTROL_PLANE_FILES = {
    "AGENTS.md",
    "ORCHESTRATOR.md",
    "WORKFLOW.md",
    "STATE.md",
}
CONTROL_PLANE_PREFIXES = (
    ".github/",
    "harness/",
    "scripts/",
    "skills/",
    "state/",
    "status/",
    "templates/",
)
CODE_SUFFIXES = {
    ".bash",
    ".c",
    ".cc",
    ".cpp",
    ".go",
    ".js",
    ".jsx",
    ".mjs",
    ".py",
    ".rs",
    ".sh",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}
NON_MATERIAL_SUFFIXES = {".md", ".markdown", ".rst", ".txt"}
COMPLETE_STATES_REQUIRING_FINALIZER = {"Human Review", "Done", "Merging"}
PASS_STATUSES = {"ok", "pass", "passed", "success", "succeeded"}
NOT_DONE_STATUSES = {"not_done", "notdone", "ready_for_human", "ready-for-human"}
BRANCH_ONLY_MODES = {"branch_only", "branch-only", "open_pr", "open-pr", "draft", "draft_only", "draft-only", "candidate", "candidate_only", "candidate-only"}
APPROVED_PUBLICATION_PERMISSION_STATUSES = {
    "approved",
    "authorized",
    "exact_approval",
    "exact_approved",
    "human_approved",
    "granted",
    "performed_by_human",
}
MISSING_PUBLICATION_PERMISSION_STATUSES = {
    "missing",
    "absent",
    "not_approved",
    "not-approved",
    "unapproved",
    "human_only",
    "human-only",
    "denied",
    "rejected",
}
LANDING_OPERATION_MARKERS = ("publication", "default_ref", "final_ref", "landing", "landed", "merge", "merged")
TERMINAL_NON_COMPLETION_STATES = {"Cancelled", "Canceled", "Duplicate", "Blocked"}
PATH_METADATA_KEYS = ("path", "paths", "changedPaths", "stagedPaths", "wouldStagePaths", "manifestPaths")
FORBIDDEN_FINALIZER_EVIDENCE_EXACT_KEYS = {"env"}
ALLOWED_SCHEMA_EVIDENCE_KEYS = {
    "claimscope",
    "claim_scope",
    "failedrequiredreads",
    "nofailedrequiredreads",
    "nonzeroexitsnormalizedaway",
    "omittedrawdiagnostics",
    "rawdiagnostics",
    "rawdiagnosticspreserved",
    "rawevidencerefs",
    "requiredreadsnormalizedaway",
    "rootauthority",
    "sessionorrunid",
    "sourceauthority",
    "sourceauthorityrefs",
    "stateauthorityrefs",
    "stderrcontradictions",
}
FORBIDDEN_FINALIZER_EVIDENCE_KEY_FRAGMENTS = (
    "apikey",
    "auth",
    "authorization",
    "credential",
    "device",
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
CLAIM_SCOPE_REQUIRED_FIELDS = (
    "subject",
    "requestedClaim",
    "proofTier",
    "verdict",
    "rawEvidenceRefs",
    "negativeControls",
    "lifecycleEvidence",
    "allowedClaims",
    "forbiddenClaims",
    "limitations",
    "stateAuthorityRefs",
    "reviewRoute",
    "reviewerEvidenceRefs",
    "finalizerIdentity",
)
CLAIM_SCOPE_PROOF_TIERS = {
    "scaffold_only",
    "preflight_only",
    "candidate",
    "runtime_parity",
    "guard_backed_target_acceptance",
    "module_acceptance",
    "project_acceptance",
    "blocked",
    "out_of_scope",
}
CLAIM_SCOPE_VERDICTS = {"PROVEN", "SCAFFOLD_ONLY", "PREFLIGHT_ONLY", "CANDIDATE", "BLOCKED", "OUT_OF_SCOPE"}
PROJECT_ACCEPTANCE_PROOF_TIER = "project_acceptance"
PROJECT_ACCEPTANCE_MARKERS = (
    "project acceptance",
    "project accepted",
    "project green",
    "project pass",
    "project passed",
    "project complete",
    "project done",
    "v1_green",
    "v2_green",
    "full green",
    "green project",
    "accepted project",
)
CLAIM_SCOPE_RAW_REF_KINDS = {
    "stdout",
    "stderr",
    "exit_code",
    "raw_trace",
    "source_hash",
    "validator_json",
    "reviewer_json",
    "lifecycle_log",
    "other",
}
PROVEN_FORBIDDEN_PROOF_TIERS = {"candidate", "scaffold_only", "preflight_only", "blocked", "out_of_scope"}
SECURITY_REVIEW_ROLE_LABELS = {
    "alpha",
    "alpha_prime",
    "alphaprime",
    "alpha-prime",
    "auditor_alpha",
    "auditor-alpha",
    "auditor_alpha_prime",
    "auditor-alpha-prime",
    "auditor_beta",
    "auditor-beta",
    "beta",
    "security",
    "security_auditor",
    "security-auditor",
    "security_code",
    "security-code",
}
LOCAL_ONLY_FINAL_DELIVERABLE_MODES = {
    "final_deliverable",
    "final-deliverable",
    "local_only_final",
    "local-only-final",
    "local_only_final_deliverable",
    "local-only-final-deliverable",
}
NOT_DONE_LOCAL_DONE_STATUSES = {"not_done", "notdone", "ready_for_human", "ready-for-human", "human_review", "human-review"}
LOCAL_ONLY_COMPLETION_MODES = {
    "branch_only",
    "candidate_only",
    "draft_only",
    "final_deliverable",
    "human_review",
    "local_only",
    "local_only_final",
    "local_only_final_deliverable",
    "local_only_human_review",
}
LOCAL_ONLY_DONE_STATUS_VALUES = NOT_DONE_LOCAL_DONE_STATUSES | {
    "complete",
    "completed",
    "done",
    "final",
    "final_done",
    "local_done",
}
READY_COMPLETION_TRANSITIONS = {"ready", "ready_reconciliation", "scope_complete", "scope_completion"}
SHA256_RE = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")
PROOF_TIER_ORDER = {
    "blocked": 0,
    "out_of_scope": 0,
    "scaffold_only": 1,
    "preflight_only": 2,
    "candidate": 3,
    "runtime_parity": 4,
    "guard_backed_target_acceptance": 5,
    "module_acceptance": 6,
    "project_acceptance": 7,
}
MODULE_ACCEPTANCE_MARKERS = (
    "module acceptance",
    "module accepted",
    "module green",
    "module pass",
    "module passed",
    "module complete",
    "module done",
    "accepted module",
    "green module",
)
TARGET_ACCEPTANCE_MARKERS = (
    "guard backed target acceptance",
    "guard backed target cycle",
    "target acceptance",
    "target cycle accepted",
    "target cycle acceptance",
)
RUNTIME_PARITY_MARKERS = ("runtime parity", "runtime_parity")
FINALIZER_IDENTITY_ROLES = {"mediator_finalizer", "finalizer", "human"}


def is_required_issue(issue: dict[str, Any]) -> bool:
    return issue.get("required", True) is not False


def _as_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def normalize_exact_path(raw: Any) -> tuple[str | None, str | None]:
    if not isinstance(raw, str):
        return None, "path is not a string"
    text = raw.strip()
    if not text:
        return None, "path is empty"
    if text.startswith("/"):
        return None, f"path is absolute: {text}"
    if text.endswith("/") or text.endswith("\\"):
        return None, f"path is not exact: {text}"
    if any(ch in text for ch in "*?[]"):
        return None, f"path contains glob/wildcard syntax: {text}"
    normalized = posixpath.normpath(text.replace("\\", "/"))
    if normalized in {"", "."}:
        return None, "path resolves to repository root"
    if normalized == ".." or normalized.startswith("../") or "/../" in f"/{normalized}/":
        return None, f"path escapes repository: {text}"
    if normalized.endswith("/"):
        return None, f"path is not exact: {text}"
    return normalized, None


def normalize_path_list(values: Iterable[Any]) -> tuple[list[str], list[str]]:
    paths: list[str] = []
    errors: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized, err = normalize_exact_path(value)
        if err:
            errors.append(err)
            continue
        assert normalized is not None
        if normalized not in seen:
            paths.append(normalized)
            seen.add(normalized)
    return paths, errors


def path_is_control_plane(path: str) -> bool:
    return path in CONTROL_PLANE_FILES or any(path.startswith(prefix) for prefix in CONTROL_PLANE_PREFIXES)


def path_is_private_memory(path: str) -> bool:
    return path == "MEMORY.md" or path.startswith("memory/")


def path_is_broad_evidence_tree(path: str) -> bool:
    if path in {"evidence", "handoffs"}:
        return True
    if path.startswith("evidence/") and not Path(path).suffix:
        return True
    if path.startswith("handoffs/") and not Path(path).suffix:
        return True
    return any(marker in path for marker in ("/live-evidence", "/redacted-final", "/evidence-tree", "/raw-evidence-tree"))


def path_is_material(path: str) -> bool:
    suffix = Path(path).suffix.lower()
    return path_is_control_plane(path) or suffix in CODE_SUFFIXES or suffix not in NON_MATERIAL_SUFFIXES


def _collect_paths_from_mapping(mapping: dict[str, Any], keys: Iterable[str]) -> list[Any]:
    out: list[Any] = []
    for key in keys:
        out.extend(_as_list(mapping.get(key)))
    return out


def issue_declared_paths(issue: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return normalized exact paths declared on the issue/status metadata."""

    raw: list[Any] = []
    raw.extend(
        _collect_paths_from_mapping(
            issue,
            (
                "allowedPaths",
                "changedPaths",
                "stagedPaths",
                "manifestPaths",
                "paths",
                "path",
                "deliverablesFile",
            ),
        )
    )
    status = issue.get("status") if isinstance(issue.get("status"), dict) else {}
    raw.extend(_collect_paths_from_mapping(status, ("allowedPaths", "changedPaths", "stagedPaths", "manifestPaths", "paths", "path")))
    finalizer = status.get("finalizer") if isinstance(status.get("finalizer"), dict) else {}
    raw.extend(_collect_paths_from_mapping(finalizer, PATH_METADATA_KEYS))
    local = finalizer.get("localOnly") if isinstance(finalizer.get("localOnly"), dict) else {}
    raw.extend(_collect_paths_from_mapping(local, PATH_METADATA_KEYS))
    return normalize_path_list(raw)


def _explicit_bool(issue: dict[str, Any], key: str) -> bool | None:
    if key in issue:
        return issue.get(key) is True
    status = issue.get("status") if isinstance(issue.get("status"), dict) else {}
    if key in status:
        return status.get(key) is True
    finalizer = status.get("finalizer") if isinstance(status.get("finalizer"), dict) else {}
    if key in finalizer:
        return finalizer.get(key) is True
    return None


def _explicit_finalizer_not_required(issue: dict[str, Any]) -> bool:
    for key in ("finalizerNotRequired", "noFinalizerRequired"):
        explicit = _explicit_bool(issue, key)
        if explicit is True:
            return True
    for key in ("finalizerRequired", "requiresFinalizer"):
        if key in issue and issue.get(key) is False:
            return True
        status = issue.get("status") if isinstance(issue.get("status"), dict) else {}
        if key in status and status.get(key) is False:
            return True
    return False


def local_only_exception(issue: dict[str, Any], *, strict_completion: bool = True) -> dict[str, Any]:
    status = issue.get("status") if isinstance(issue.get("status"), dict) else {}
    finalizer = status.get("finalizer") if isinstance(status.get("finalizer"), dict) else {}
    local = finalizer.get("localOnly")
    if local in (None, False, ""):
        return {"present": False, "ok": False, "reason": "localOnly exception absent"}
    if local is True:
        return {"present": True, "ok": False, "reason": "localOnly must be an object with exact paths and rationale"}
    if not isinstance(local, dict):
        return {"present": True, "ok": False, "reason": "localOnly must be an object"}

    paths, path_errors = normalize_path_list(_collect_paths_from_mapping(local, PATH_METADATA_KEYS))
    rationale = str(local.get("rationale") or local.get("reason") or "").strip()
    completion_mode = str(local.get("completionMode") or local.get("completion_mode") or "").strip()
    done_status = str(local.get("doneStatus") or local.get("done_status") or "").strip()
    if path_errors:
        return {"present": True, "ok": False, "reason": "; ".join(path_errors), "paths": paths, "rationale": rationale}
    if not paths:
        return {"present": True, "ok": False, "reason": "localOnly requires at least one exact path", "paths": paths, "rationale": rationale}
    if not rationale:
        return {"present": True, "ok": False, "reason": "localOnly requires a rationale", "paths": paths, "rationale": rationale}
    if strict_completion and not completion_mode:
        return {"present": True, "ok": False, "reason": "localOnly requires completionMode", "paths": paths, "rationale": rationale}
    if strict_completion and not done_status:
        return {"present": True, "ok": False, "reason": "localOnly requires doneStatus", "paths": paths, "rationale": rationale}
    if any(path_is_control_plane(path) for path in paths):
        return {
            "present": True,
            "ok": False,
            "reason": "localOnly cannot bypass harness/control-plane paths",
            "paths": paths,
            "rationale": rationale,
        }
    if any(path_is_private_memory(path) for path in paths):
        return {
            "present": True,
            "ok": False,
            "reason": "localOnly cannot bypass private memory paths",
            "paths": paths,
            "rationale": rationale,
        }
    if any(path_is_broad_evidence_tree(path) for path in paths):
        return {
            "present": True,
            "ok": False,
            "reason": "localOnly cannot bypass broad evidence trees",
            "paths": paths,
            "rationale": rationale,
        }

    allowed_paths, allowed_errors = normalize_path_list(_as_list(issue.get("allowedPaths")))
    if allowed_errors:
        return {
            "present": True,
            "ok": False,
            "reason": f"issue.allowedPaths invalid: {'; '.join(allowed_errors)}",
            "paths": paths,
            "rationale": rationale,
        }
    if allowed_paths and any(path_is_control_plane(path) for path in allowed_paths):
        return {
            "present": True,
            "ok": False,
            "reason": "localOnly cannot bypass issue.allowedPaths containing harness/control-plane paths",
            "paths": paths,
            "rationale": rationale,
        }
    if allowed_paths and any(path_is_private_memory(path) for path in allowed_paths):
        return {
            "present": True,
            "ok": False,
            "reason": "localOnly cannot bypass issue.allowedPaths containing private memory paths",
            "paths": paths,
            "rationale": rationale,
        }
    if allowed_paths and any(path_is_broad_evidence_tree(path) for path in allowed_paths):
        return {
            "present": True,
            "ok": False,
            "reason": "localOnly cannot bypass issue.allowedPaths containing broad evidence trees",
            "paths": paths,
            "rationale": rationale,
        }
    if allowed_paths and not set(paths).issubset(set(allowed_paths)):
        return {
            "present": True,
            "ok": False,
            "reason": "localOnly paths must be a subset of issue.allowedPaths",
            "paths": paths,
            "rationale": rationale,
        }

    mode_token = _normalized_token(completion_mode)
    done_token = _normalized_token(done_status)
    if mode_token not in LOCAL_ONLY_COMPLETION_MODES:
        return {
            "present": True,
            "ok": False,
            "reason": "localOnly completionMode is not one of the constrained allowed values",
            "paths": paths,
            "rationale": rationale,
            "completionMode": completion_mode,
            "doneStatus": done_status,
        }
    if done_token not in LOCAL_ONLY_DONE_STATUS_VALUES:
        return {
            "present": True,
            "ok": False,
            "reason": "localOnly doneStatus is not one of the constrained allowed values",
            "paths": paths,
            "rationale": rationale,
            "completionMode": completion_mode,
            "doneStatus": done_status,
        }
    final_deliverable = mode_token in LOCAL_ONLY_FINAL_DELIVERABLE_MODES and done_token not in NOT_DONE_LOCAL_DONE_STATUSES
    rendered_done_status = done_status if final_deliverable else "NOT_DONE"
    return {
        "present": True,
        "ok": True,
        "reason": "localOnly exception accepted",
        "paths": paths,
        "rationale": rationale,
        "completionMode": completion_mode,
        "doneStatus": rendered_done_status,
        "finalDeliverable": final_deliverable,
    }


def issue_requires_finalizer(issue: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(issue, dict):
        return True, "issue metadata is unreadable"
    if not is_required_issue(issue):
        return False, "issue.required=false"
    state = str(issue.get("state") or "")
    if state in TERMINAL_NON_COMPLETION_STATES:
        return False, f"terminal non-completion state {state}"

    explicit_required = _explicit_bool(issue, "finalizerRequired")
    if explicit_required is True or _explicit_bool(issue, "requiresFinalizer") is True:
        return True, "explicit finalizerRequired=true"

    kind = str(issue.get("kind") or "task")
    paths, path_errors = issue_declared_paths(issue)
    if path_errors:
        return True, "issue has invalid path metadata"
    if any(path_is_control_plane(path) for path in paths):
        return True, "harness/control-plane path in issue scope"
    if _explicit_finalizer_not_required(issue):
        if paths and all(not path_is_material(path) for path in paths):
            return False, "safe explicit non-material finalizer exception"
        return True, "explicit finalizer exception is unsafe without non-material exact paths"
    if kind == "epic":
        return False, "epic finalization is established by required children unless explicit or path-scoped"

    issue_id = str(issue.get("id") or "")
    if issue_id.startswith(("fwk-", "fwk_")):
        return True, "framework/control-plane issue id defaults finalizer-required"
    if paths and any(path_is_material(path) for path in paths):
        return True, "material path in issue scope"
    if isinstance(((issue.get("status") or {}) if isinstance(issue.get("status"), dict) else {}).get("finalizer"), dict):
        return True, "finalizer metadata present"
    return False, "no material finalizer-required scope detected"


def _non_empty_errors(errors: Any) -> bool:
    if errors in (None, "", [], {}):
        return False
    if isinstance(errors, list):
        return any(item not in (None, "", [], {}) for item in errors)
    if isinstance(errors, dict):
        return any(value not in (None, "", [], {}) for value in errors.values())
    if isinstance(errors, str):
        return bool(errors.strip())
    return True


def _normalized_evidence_key(key: str) -> str:
    return "".join(char for char in key.lower() if char.isalnum())


def forbidden_finalizer_evidence_keys(value: Any) -> list[str]:
    forbidden: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = _normalized_evidence_key(str(key))
            if normalized not in ALLOWED_SCHEMA_EVIDENCE_KEYS and (
                normalized in FORBIDDEN_FINALIZER_EVIDENCE_EXACT_KEYS or any(
                fragment in normalized for fragment in FORBIDDEN_FINALIZER_EVIDENCE_KEY_FRAGMENTS
                )
            ):
                forbidden.add(str(key))
            forbidden.update(forbidden_finalizer_evidence_keys(child))
    elif isinstance(value, list):
        for child in value:
            forbidden.update(forbidden_finalizer_evidence_keys(child))
    return sorted(forbidden)


def _commit_values(finalizer: dict[str, Any]) -> list[str]:
    vals = []
    for key in ("subjectCommit", "subjectCommitOid", "commit", "headCommit"):
        value = str(finalizer.get(key) or "").strip()
        if value and value not in vals:
            vals.append(value)
    return vals


def _normalized_token(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def _normalized_claim_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _truthy(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "y", "1", "passed", "pass", "ok", "success", "succeeded"}
    return False


def _dict_value(mapping: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _entry_label(entry: Any, default: str = "human-only operation") -> str:
    if isinstance(entry, dict):
        return str(entry.get("operation") or entry.get("name") or entry.get("id") or default).strip() or default
    return str(entry).strip() or default


def _entry_explicitly_performed(entry: Any) -> bool:
    if isinstance(entry, dict):
        if _truthy(entry.get("performed")) or _truthy(entry.get("done")) or _truthy(entry.get("completed")):
            return True
        status = _normalized_token(entry.get("status"))
        return status in {"performed", "done", "complete", "completed", "satisfied"}
    return False


def pending_human_only_operations(human_only: Any) -> list[str]:
    pending: list[str] = []
    if isinstance(human_only, list):
        for entry in human_only:
            if isinstance(entry, dict):
                if not _entry_explicitly_performed(entry):
                    pending.append(_entry_label(entry))
            elif str(entry).strip():
                pending.append(str(entry).strip())
    elif isinstance(human_only, dict):
        for entry in _as_list(human_only.get("notPerformed")):
            if isinstance(entry, dict):
                if not _entry_explicitly_performed(entry):
                    pending.append(_entry_label(entry))
            elif str(entry).strip():
                pending.append(str(entry).strip())
    return pending[:20]


def _permission_tokens(*values: Any) -> list[str]:
    tokens: list[str] = []
    for value in values:
        if value in (None, ""):
            continue
        if isinstance(value, dict):
            if _truthy(value.get("approved")) or _truthy(value.get("exactApproval")):
                tokens.append("approved")
            if _truthy(value.get("rejected")) or _truthy(value.get("denied")):
                tokens.append("denied")
            for key in ("status", "permission", "decision", "state"):
                token = _normalized_token(value.get(key))
                if token:
                    tokens.append(token)
        else:
            token = _normalized_token(value)
            if token:
                tokens.append(token)
    return tokens


def _operation_is_landing(name: str) -> bool:
    normalized = _normalized_token(name).replace("-", "_")
    return any(marker in normalized for marker in LANDING_OPERATION_MARKERS)


def explicit_human_performed_landing(finalizer: dict[str, Any], publication: dict[str, Any], human_only: Any) -> bool:
    if any(
        _truthy(finalizer.get(key))
        for key in ("humanPerformedLanding", "publicationPerformedByHuman", "mergePerformedByHuman", "defaultRefLandingPerformedByHuman")
    ):
        return True
    if any(_truthy(publication.get(key)) for key in ("performedByHuman", "humanPerformed", "landedByHuman")):
        return True
    if isinstance(human_only, dict):
        for entry in _as_list(human_only.get("performedByHuman")):
            if isinstance(entry, dict):
                if _entry_explicitly_performed(entry) and _operation_is_landing(_entry_label(entry)):
                    return True
            elif _operation_is_landing(str(entry)):
                return True
    if isinstance(human_only, list):
        for entry in human_only:
            if isinstance(entry, dict) and _entry_explicitly_performed(entry) and _operation_is_landing(_entry_label(entry)):
                return True
    return False


def explicit_completion_exception(finalizer: dict[str, Any]) -> dict[str, Any]:
    exception = _dict_value(finalizer, "explicitCompletionException", "completionException", "approvedCompletionMode")
    if not exception:
        return {"present": False, "ok": False, "reason": "no explicit completion exception"}
    paths, path_errors = normalize_path_list(_collect_paths_from_mapping(exception, PATH_METADATA_KEYS))
    rationale = str(exception.get("rationale") or exception.get("reason") or "").strip()
    approved = _truthy(exception.get("approved")) or _truthy(exception.get("exactApproval"))
    if path_errors:
        return {"present": True, "ok": False, "reason": "; ".join(path_errors), "paths": paths}
    if not approved:
        return {"present": True, "ok": False, "reason": "completion exception requires exact approved=true metadata", "paths": paths}
    if not rationale:
        return {"present": True, "ok": False, "reason": "completion exception requires rationale", "paths": paths}
    if not (paths or str(exception.get("ref") or exception.get("branch") or "").strip()):
        return {"present": True, "ok": False, "reason": "completion exception requires exact path or ref metadata", "paths": paths}
    return {"present": True, "ok": True, "reason": "explicit completion exception accepted", "paths": paths, "rationale": rationale}


def done_lifecycle_evidence(finalizer: dict[str, Any]) -> dict[str, Any]:
    """Validate strict branch/PR/default-ref completion semantics for Done.

    This is intentionally metadata-only: it inspects declared lifecycle facts and
    never queries GitHub or reads tokens.  Missing publication/default-ref proof
    is not a success; callers should surface the result as ready-for-human and
    NOT_DONE instead of advancing to Done.
    """

    completion_status = _normalized_token(finalizer.get("doneStatus", finalizer.get("completionStatus")))
    if completion_status in NOT_DONE_STATUSES or _truthy(finalizer.get("readyForHuman")):
        return {
            "ok": False,
            "status": "NOT_DONE",
            "readyForHuman": True,
            "reason": "status.finalizer explicitly reports NOT_DONE/ready-for-human",
        }

    mode = _normalized_token(finalizer.get("completionMode", finalizer.get("handoffMode")))
    if mode in BRANCH_ONLY_MODES:
        exception = explicit_completion_exception(finalizer)
        if not exception.get("ok"):
            return {
                "ok": False,
                "status": "NOT_DONE",
                "readyForHuman": True,
                "reason": f"branch/PR/candidate mode '{mode}' is not Done without an explicit approved completion exception: {exception.get('reason')}",
                "completionMode": mode,
            }
        return {"ok": True, "status": "exception", "completionMode": mode, "completionException": exception}

    branch_state = _dict_value(finalizer, "branch", "branchState", "branchPrState")
    pr_state = _dict_value(finalizer, "pullRequest", "pr", "prState")
    branch_disposition = _normalized_token(branch_state.get("state", branch_state.get("disposition")))
    pr_disposition = _normalized_token(pr_state.get("state", pr_state.get("disposition")))
    pr_merged = _truthy(pr_state.get("merged")) or pr_disposition == "merged"
    if branch_disposition in {"branch_only", "branch-only", "candidate", "draft"}:
        return {
            "ok": False,
            "status": "NOT_DONE",
            "readyForHuman": True,
            "reason": "branch-only/candidate branch state is review handoff, not Done",
            "branchState": branch_disposition,
        }
    if pr_state and not pr_merged:
        return {
            "ok": False,
            "status": "NOT_DONE",
            "readyForHuman": True,
            "reason": "open or unmerged PR is review handoff, not Done",
            "prState": pr_disposition or "unmerged",
        }

    publication = _dict_value(finalizer, "publication", "defaultRefStatus", "finalRefStatus")
    publication_status = _normalized_token(publication.get("status"))
    default_ref_updated = (
        _truthy(finalizer.get("defaultRefUpdated"))
        or _truthy(finalizer.get("finalRefUpdated"))
        or _truthy(finalizer.get("finalRefProven"))
        or _truthy(publication.get("defaultRefUpdated"))
        or _truthy(publication.get("finalRefUpdated"))
        or _truthy(publication.get("finalRefProven"))
        or publication_status in {"default_ref_landed", "final_ref_landed", "landed", "merged", "published", "updated"}
    )
    if not default_ref_updated:
        return {
            "ok": False,
            "status": "NOT_DONE",
            "readyForHuman": True,
            "reason": "missing proof that the accepted artifact landed on the authorized destination/default ref",
        }

    human_only = finalizer.get("humanOnlyOperations")
    pending_human_only = pending_human_only_operations(human_only)
    if pending_human_only:
        return {
            "ok": False,
            "status": "NOT_DONE",
            "readyForHuman": True,
            "reason": "human-only operations remain pending",
            "pendingHumanOnlyOperations": pending_human_only,
        }

    permission_tokens = _permission_tokens(
        finalizer.get("publicationPermission"),
        finalizer.get("mergePermission"),
        finalizer.get("publicationApproval"),
        finalizer.get("mergeApproval"),
        publication.get("permission"),
        publication.get("publicationPermission"),
        publication.get("mergePermission"),
        publication.get("approval"),
    )
    if any(token in MISSING_PUBLICATION_PERMISSION_STATUSES for token in permission_tokens):
        return {
            "ok": False,
            "status": "NOT_DONE",
            "readyForHuman": True,
            "reason": "publication/merge permission missing or human-only",
        }
    permission_approved = any(token in APPROVED_PUBLICATION_PERMISSION_STATUSES for token in permission_tokens)
    if not permission_approved and not explicit_human_performed_landing(finalizer, publication, human_only):
        return {
            "ok": False,
            "status": "NOT_DONE",
            "readyForHuman": True,
            "reason": "publication/merge permission is absent or not exactly approved",
        }

    return {"ok": True, "status": "landed", "defaultRefUpdated": True}


def finalizer_path_scope(issue: dict[str, Any], finalizer: dict[str, Any], *, reject_control_plane: bool = False) -> dict[str, Any]:
    path_values: list[Any] = _collect_paths_from_mapping(finalizer, PATH_METADATA_KEYS)
    local = finalizer.get("localOnly") if isinstance(finalizer.get("localOnly"), dict) else {}
    path_values.extend(_collect_paths_from_mapping(local, PATH_METADATA_KEYS))
    finalizer_paths, path_errors = normalize_path_list(path_values)
    if path_errors:
        return {"ok": False, "reason": "; ".join(path_errors), "paths": finalizer_paths}

    allowed_paths, allowed_errors = normalize_path_list(_as_list(issue.get("allowedPaths")))
    if allowed_errors:
        return {"ok": False, "reason": "; ".join(allowed_errors), "paths": finalizer_paths}
    if allowed_paths:
        off_scope_paths = sorted(set(finalizer_paths) - set(allowed_paths))
        if off_scope_paths:
            return {
                "ok": False,
                "reason": "status.finalizer path metadata is outside issue.allowedPaths",
                "paths": finalizer_paths,
                "offScopePaths": off_scope_paths[:20],
            }
    if reject_control_plane:
        control_plane_paths = sorted(path for path in set(finalizer_paths) if path_is_control_plane(path))
        if control_plane_paths:
            return {
                "ok": False,
                "reason": "status.finalizer local/non-required path metadata cannot include harness/control-plane paths",
                "paths": finalizer_paths,
                "controlPlanePaths": control_plane_paths[:20],
            }
    return {"ok": True, "paths": finalizer_paths}


def _claim_scope_object(finalizer: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("claimScope", "claim_scope"):
        value = finalizer.get(key)
        if isinstance(value, dict):
            return value
    return None


def _non_empty_string_list(value: Any) -> bool:
    return isinstance(value, list) and any(str(item).strip() for item in value)


def _normalize_role(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def _ref_path(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("path") or value.get("artifactRef") or value.get("ref") or "").strip()
    return str(value or "").strip()


def _normalize_claim_ref(value: Any) -> tuple[str | None, str | None]:
    path = _ref_path(value)
    if not path:
        return None, "ref path is empty"
    return normalize_exact_path(path)


def _entry_commit_values(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    vals: list[str] = []
    for key in ("subjectCommit", "subjectCommitOid", "commit", "headCommit"):
        raw = str(value.get(key) or "").strip()
        if raw and raw not in vals:
            vals.append(raw)
    return vals


def _entry_destination_ref(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("destinationRef") or value.get("ref") or value.get("targetRef") or "").strip()


def _entry_branch_or_na(value: dict[str, Any], expected: str, *, key: str) -> str | None:
    actual = str(value.get(key) or "").strip()
    reason = str(value.get(f"{key}NotApplicableReason") or value.get(f"{key}_not_applicable_reason") or "").strip()
    if expected:
        if not actual and not reason:
            return f"{key} or {key}NotApplicableReason is required"
        if actual and actual != expected:
            return f"{key} {actual} does not match expected {expected}"
    return None


def _entry_matches_claim_identity(
    entry: Any,
    *,
    label: str,
    index: int | None,
    subject: str,
    requested_claim: str,
    finalizer_commits: list[str],
    destination_ref: str,
    expected_branch: str = "",
    expected_worktree: str = "",
) -> list[str]:
    rendered_label = label if index is None else f"{label}[{index}]"
    if not isinstance(entry, dict):
        return [f"claimScope.{rendered_label} must be an object with identity metadata"]

    errors: list[str] = []
    if str(entry.get("subject") or "").strip() != subject:
        errors.append(f"claimScope.{rendered_label}.subject must match claimScope.subject")
    if str(entry.get("requestedClaim") or "").strip() != requested_claim:
        errors.append(f"claimScope.{rendered_label}.requestedClaim must match claimScope.requestedClaim")

    if finalizer_commits:
        commits = _entry_commit_values(entry)
        if not commits:
            errors.append(f"claimScope.{rendered_label} must carry subjectCommit/subjectCommitOid metadata")
        elif not any(commit in finalizer_commits for commit in commits):
            errors.append(f"claimScope.{rendered_label} subject commit does not match status.finalizer commit")

    if destination_ref:
        ref = _entry_destination_ref(entry)
        if not ref:
            errors.append(f"claimScope.{rendered_label} must carry destinationRef/ref metadata")
        elif not _ref_matches(ref, destination_ref):
            errors.append(f"claimScope.{rendered_label} destinationRef/ref does not match status.finalizer destinationRef")

    branch_error = _entry_branch_or_na(entry, expected_branch, key="branch")
    if branch_error:
        errors.append(f"claimScope.{rendered_label}.{branch_error}")
    worktree_error = _entry_branch_or_na(entry, expected_worktree, key="worktree")
    if worktree_error:
        errors.append(f"claimScope.{rendered_label}.{worktree_error}")

    return errors


def _entry_review_route_errors(
    entry: Any,
    *,
    label: str,
    index: int | None,
    expected_profile: str,
    expected_tier: str,
) -> list[str]:
    rendered_label = label if index is None else f"{label}[{index}]"
    if not isinstance(entry, dict):
        return []
    route = entry.get("reviewRoute") if isinstance(entry.get("reviewRoute"), dict) else {}
    profile = _normalized_token(route.get("reviewProfile") or entry.get("reviewProfile"))
    tier = _normalized_token(route.get("reviewTier") or entry.get("reviewTier"))
    errors: list[str] = []
    if not profile:
        errors.append(f"claimScope.{rendered_label} must carry reviewProfile/reviewRoute.reviewProfile")
    elif expected_profile and profile != expected_profile:
        errors.append(f"claimScope.{rendered_label} reviewProfile does not match claimScope.reviewRoute")
    if not tier:
        errors.append(f"claimScope.{rendered_label} must carry reviewTier/reviewRoute.reviewTier")
    elif expected_tier and tier != expected_tier:
        errors.append(f"claimScope.{rendered_label} reviewTier does not match claimScope.reviewRoute")
    return errors


def _stringify_tree(value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            values.append(str(key))
            values.extend(_stringify_tree(child))
    elif isinstance(value, list):
        for child in value:
            values.extend(_stringify_tree(child))
    elif value is not None:
        values.append(str(value))
    return values


def _tree_contains_marker(value: Any, markers: Iterable[str]) -> bool:
    lowered = "\n".join(_stringify_tree(value)).lower()
    return any(marker in lowered for marker in markers)


def _limitations_mention_derived_non_authoritative(scope: dict[str, Any]) -> bool:
    limitations = " ".join(str(item).lower() for item in _as_list(scope.get("limitations")))
    return "derived" in limitations and ("non-authoritative" in limitations or "non_authoritative" in limitations or "nonauthoritative" in limitations)


def _legacy_wiggum_reason(scope: dict[str, Any]) -> str | None:
    home = openclaw_home()
    if _tree_contains_marker(
        scope,
        (
            str(home / "active-tasks.json"),
            str(home / "scripts" / "run-auditor.sh"),
            str(home / "wiggum") + "/",
        ),
    ):
        return "WIGGUM_LEGACY_DIAGNOSTIC_ONLY: installed legacy auditor/Wiggum paths are diagnostic-only, not acceptance evidence"
    authority_values = []
    for key in ("stateAuthorityRefs", "sourceAuthorityRefs", "sourceAuthority", "rootAuthority"):
        authority_values.extend(_as_list(scope.get(key)))
    if _tree_contains_marker(
        authority_values,
        (
            "legacy_active_tasks",
            "legacy active tasks",
            "wiggum",
            str(home / "scripts" / "run-auditor.sh"),
            str(home / "wiggum") + "/",
        ),
    ):
        return "WIGGUM_LEGACY_DIAGNOSTIC_ONLY: wiggum/legacy_active_tasks cannot count as acceptance authority"
    if _tree_contains_marker(authority_values, ("state/active-tasks.json",)):
        if not _limitations_mention_derived_non_authoritative(scope):
            return "WIGGUM_LEGACY_DIAGNOSTIC_ONLY: state/active-tasks.json is derived/non-authoritative and must be explicitly limited"
        if _tree_contains_marker(authority_values, ("sourceauthority", "rootauthority", "source authority", "root authority")):
            return "WIGGUM_LEGACY_DIAGNOSTIC_ONLY: state/active-tasks.json cannot be cited as source authority"
    return None


def _raw_diagnostic_failure_reason(scope: dict[str, Any]) -> str | None:
    proof_tier = str(scope.get("proofTier") or "").strip()
    verdict = str(scope.get("verdict") or "").strip().upper()
    if proof_tier in {"blocked", "out_of_scope"} or verdict in {"BLOCKED", "OUT_OF_SCOPE"}:
        return None

    diagnostics = {
        "rawDiagnostics": scope.get("rawDiagnostics"),
        "omittedRawDiagnostics": scope.get("omittedRawDiagnostics"),
    }
    markers = (
        "failed read",
        "failed reads",
        "failed required read",
        "required-read failure",
        "required read failure",
        "missing required file",
        "nonzero exit",
        "non-zero exit",
        "omitted stderr",
        "stderr omitted",
        "stderr contradiction",
        "provenance mismatch",
        "normalized away",
        "normalized away diagnostics",
        "normalized-away diagnostics",
        "success marker plus required-read failure",
        "success marker plus required read failure",
    )
    for key, value in diagnostics.items():
        if _tree_contains_marker(value, markers):
            return f"RAW_EVIDENCE_BLOCKED: {key} contains failed required diagnostics that cannot be normalized away"
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict):
                    for exit_key in ("exitCode", "exit_code", "returnCode", "return_code"):
                        exit_value = entry.get(exit_key)
                        if isinstance(exit_value, int) and exit_value != 0:
                            return f"RAW_EVIDENCE_BLOCKED: {key} contains nonzero exit evidence"
    return None


def _raw_failure_markers() -> tuple[str, ...]:
    return (
        "failed read",
        "failed reads",
        "failed required read",
        "required-read failure",
        "required read failure",
        "missing required file",
        "nonzero exit",
        "non-zero exit",
        "omitted stderr",
        "stderr omitted",
        "stderr contradiction",
        "provenance mismatch",
        "normalized away",
        "normalized away diagnostics",
        "normalized-away diagnostics",
        "success marker plus required-read failure",
        "success marker plus required read failure",
    )


def _structured_raw_validation_ok(ref: dict[str, Any]) -> bool:
    validation = ref.get("validation") or ref.get("validationResult") or ref.get("structuredValidationResult")
    if not isinstance(validation, dict):
        return False
    status = _normalized_token(validation.get("status") or validation.get("validationStatus"))
    if status not in {"accepted", "passed", "pass", "ok", "valid", "validated"}:
        return False
    negative_flags = (
        "failedRequiredReads",
        "failed_required_reads",
        "stderrContradictions",
        "stderr_contradictions",
        "nonzeroExitsNormalizedAway",
        "nonzero_exits_normalized_away",
        "requiredReadsNormalizedAway",
        "required_reads_normalized_away",
    )
    if any(validation.get(key) not in (False, 0, None, "") for key in negative_flags):
        return False
    positive_flags = ("noFailedRequiredReads", "rawDiagnosticsPreserved")
    return any(validation.get(key) is True for key in positive_flags)


def _raw_evidence_local_file_errors(scope: dict[str, Any], refs: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    if str(scope.get("proofTier") or "").strip() in {"blocked", "out_of_scope"}:
        return errors
    if str(scope.get("verdict") or "").strip().upper() in {"BLOCKED", "OUT_OF_SCOPE"}:
        return errors

    for index, ref in enumerate(refs):
        raw_path = str(ref.get("path") or "").strip()
        if not raw_path:
            continue
        local_path = WORKSPACE_ROOT / raw_path
        if local_path.exists() and local_path.is_file():
            try:
                data = local_path.read_bytes()
            except OSError as exc:
                errors.append(f"claimScope.rawEvidenceRefs[{index}].path could not be read for raw validation: {exc}")
                continue
            expected_sha = str(ref.get("sha256") or "").removeprefix("sha256:").strip().lower()
            if expected_sha:
                actual_sha = hashlib.sha256(data).hexdigest()
                if actual_sha != expected_sha:
                    errors.append(f"claimScope.rawEvidenceRefs[{index}].sha256 does not match referenced local artifact")
                    continue
            text = data[:1_000_000].decode("utf-8", errors="replace").lower()
            if any(marker in text for marker in _raw_failure_markers()):
                errors.append(
                    f"RAW_EVIDENCE_BLOCKED: claimScope.rawEvidenceRefs[{index}].path contains failed required diagnostics not preserved in rawDiagnostics"
                )
            continue

        if not ref.get("sha256") or not _structured_raw_validation_ok(ref):
            errors.append(
                f"claimScope.rawEvidenceRefs[{index}] local artifact is not readable; require sha256 plus structured validation preserving raw diagnostics"
            )
    return errors


def _raw_ref_has_provenance(ref: Any) -> bool:
    return raw_evidence_integrity.raw_ref_has_provenance(ref)


def _raw_evidence_provenance_errors(scope: dict[str, Any], proof_tier: str, verdict: str) -> list[str]:
    if proof_tier in {"blocked", "out_of_scope"} or verdict in {"BLOCKED", "OUT_OF_SCOPE"}:
        return []

    errors: list[str] = []
    refs = scope.get("rawEvidenceRefs")
    if isinstance(refs, list) and not any(_raw_ref_has_provenance(ref) for ref in refs):
        errors.append("claimScope.rawEvidenceRefs must include source_hash, run identity, or exit_code provenance for success claims")

    for key in ("rawDiagnostics", "omittedRawDiagnostics"):
        value = scope.get(key)
        if value in (None, "", [], {}):
            continue
        entries = value if isinstance(value, list) else [value]
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                errors.append(f"claimScope.{key}[{index}] must be structured diagnostics with provenance, not marker-only text")
                continue
            if not _raw_ref_has_provenance(entry):
                errors.append(f"claimScope.{key}[{index}] must carry source hash, run identity, or exit evidence")
        if key == "omittedRawDiagnostics":
            limitations = " ".join(str(item).lower() for item in _as_list(scope.get("limitations")))
            if "omitted" not in limitations and "raw diagnostic" not in limitations:
                errors.append("claimScope.omittedRawDiagnostics must be carried as a sticky limitation")
    return errors


def _raw_evidence_artifact_root(issue: dict[str, Any], finalizer: dict[str, Any], scope: dict[str, Any]) -> Path:
    for value in (
        issue.get("workspace"),
        issue.get("worktree"),
        finalizer.get("worktree"),
        finalizer.get("workspace"),
        scope.get("worktree"),
        scope.get("workspace"),
    ):
        text = str(value or "").strip()
        if text:
            return Path(text).expanduser().resolve()
    return WORKSPACE_ROOT


def _validate_raw_evidence_refs(
    issue: dict[str, Any],
    finalizer: dict[str, Any],
    scope: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    artifact_root = _raw_evidence_artifact_root(issue, finalizer, scope)

    def legacy_reason(value: Any) -> str | None:
        carrier = {
            "rawEvidenceRefs": [value],
            "limitations": scope.get("limitations"),
        }
        return _legacy_wiggum_reason(carrier)

    return raw_evidence_integrity.validate_raw_evidence_refs(
        scope.get("rawEvidenceRefs"),
        artifact_root=artifact_root,
        label="claimScope.rawEvidenceRefs",
        allowed_kinds=CLAIM_SCOPE_RAW_REF_KINDS,
        legacy_reason=legacy_reason,
    )


def _validate_required_ref_block(scope: dict[str, Any], key: str) -> list[str]:
    value = scope.get(key)
    errors: list[str] = []
    if not isinstance(value, dict):
        return [f"claimScope.{key} must be an object"]
    if not isinstance(value.get("required"), bool):
        errors.append(f"claimScope.{key}.required must be boolean")
    refs = value.get("refs")
    if not isinstance(refs, list):
        errors.append(f"claimScope.{key}.refs must be a list")
        refs = []
    reason = value.get("notApplicableReason")
    if not isinstance(reason, str):
        errors.append(f"claimScope.{key}.notApplicableReason must be a string")
        reason = ""
    required = value.get("required") is True
    if required and not refs:
        errors.append(f"claimScope.{key}.refs must be non-empty when required=true")
    if not required and not refs and not reason.strip():
        errors.append(f"claimScope.{key}.notApplicableReason must be non-empty when refs are empty and required=false")
    for index, ref in enumerate(refs):
        _path, err = _normalize_claim_ref(ref)
        if err:
            errors.append(f"claimScope.{key}.refs[{index}] {err}")
    return errors


def _validate_review_route(issue: dict[str, Any], scope: dict[str, Any]) -> list[str]:
    route = scope.get("reviewRoute")
    errors: list[str] = []
    if not isinstance(route, dict):
        return ["claimScope.reviewRoute must be an object"]

    review_tier = _normalized_token(route.get("reviewTier") or issue.get("reviewTier") or (issue.get("meta") or {}).get("reviewTier"))
    review_profile = _normalized_token(route.get("reviewProfile") or issue.get("reviewProfile") or (issue.get("meta") or {}).get("reviewProfile"))
    required_roles = {_normalize_role(role) for role in _as_list(route.get("requiredRoles")) if _normalize_role(role)}
    completed_roles = {_normalize_role(role) for role in _as_list(route.get("completedRoles")) if _normalize_role(role)}

    if route.get("orchestratorCountedAsReviewer") is not False:
        errors.append("claimScope.reviewRoute.orchestratorCountedAsReviewer must be false")

    if review_tier == "primary_checker_mediator":
        expected = {"primary", "checker", "mediator"}
        if not expected.issubset(required_roles):
            errors.append("claimScope.reviewRoute.requiredRoles must include primary, checker, mediator")
        if not expected.issubset(completed_roles):
            errors.append("claimScope.reviewRoute.completedRoles must include primary, checker, mediator")
        if not _as_list(scope.get("reviewerEvidenceRefs")):
            errors.append("claimScope.reviewerEvidenceRefs must be non-empty for primary_checker_mediator")
    elif review_tier == "primary_checker":
        expected = {"primary", "checker"}
        if not expected.issubset(required_roles):
            errors.append("claimScope.reviewRoute.requiredRoles must include primary, checker")
        if not expected.issubset(completed_roles):
            errors.append("claimScope.reviewRoute.completedRoles must include primary, checker")
    elif review_tier == "single_worker":
        if "primary" not in completed_roles:
            errors.append("claimScope.reviewRoute.completedRoles must include primary for single_worker")
    elif review_tier:
        errors.append("claimScope.reviewRoute.reviewTier is invalid")
    else:
        errors.append("claimScope.reviewRoute.reviewTier is required")

    if completed_roles and completed_roles.issubset(SECURITY_REVIEW_ROLE_LABELS):
        if review_profile == "ops_pipeline":
            errors.append("claimScope.reviewRoute reviewProfile=ops_pipeline cannot be satisfied by Alpha/Beta/security-only evidence")
        if review_profile == "data_validation":
            errors.append("claimScope.reviewRoute reviewProfile=data_validation cannot be satisfied by security-only evidence")

    carrier_values = [
        issue.get("carrierOverride"),
        (issue.get("status") or {}).get("carrierOverride") if isinstance(issue.get("status"), dict) else None,
        route.get("carrierOverride"),
    ]
    if review_tier in {"primary_checker", "primary_checker_mediator"} and _tree_contains_marker(carrier_values, ("no_subagents", "main_session_only_no_subagents")):
        if "checker" not in completed_roles or (review_tier == "primary_checker_mediator" and "mediator" not in completed_roles):
            errors.append("stale carrierOverride/no_subagents cannot suppress required review roles")

    for index, ref in enumerate(_as_list(scope.get("reviewerEvidenceRefs"))):
        _path, err = _normalize_claim_ref(ref)
        if err:
            errors.append(f"claimScope.reviewerEvidenceRefs[{index}] {err}")
    return errors


def _claim_mentions_project_acceptance(value: Any) -> bool:
    text = _normalized_claim_text(value)
    collapsed = text.replace(" ", "_")
    return any(marker in text or marker.replace(" ", "_") in collapsed for marker in PROJECT_ACCEPTANCE_MARKERS)


def _claim_mentions_any(value: Any, markers: Iterable[str]) -> bool:
    text = _normalized_claim_text(value)
    collapsed = text.replace(" ", "_")
    return any(marker in text or marker.replace(" ", "_") in collapsed for marker in markers)


def _minimum_proof_tier_for_claim(value: Any) -> str | None:
    if _claim_mentions_project_acceptance(value):
        return "project_acceptance"
    if _claim_mentions_any(value, MODULE_ACCEPTANCE_MARKERS):
        return "module_acceptance"
    if _claim_mentions_any(value, TARGET_ACCEPTANCE_MARKERS):
        return "guard_backed_target_acceptance"
    if _claim_mentions_any(value, RUNTIME_PARITY_MARKERS):
        return "runtime_parity"
    return None


def _validate_project_claims(scope: dict[str, Any], proof_tier: str, verdict: str) -> list[str]:
    errors: list[str] = []
    requested_claim = scope.get("requestedClaim")
    allowed_claims = [str(item).strip() for item in _as_list(scope.get("allowedClaims")) if str(item).strip()]
    forbidden_claims = [str(item).strip() for item in _as_list(scope.get("forbiddenClaims")) if str(item).strip()]

    requested_normalized = _normalized_claim_text(requested_claim)
    allowed_normalized = {_normalized_claim_text(claim) for claim in allowed_claims if _normalized_claim_text(claim)}
    if requested_normalized and requested_normalized not in allowed_normalized:
        errors.append("claimScope.allowedClaims must include the exact requestedClaim")

    tier_rank = PROOF_TIER_ORDER.get(proof_tier, -1)
    for claim in [requested_claim, *allowed_claims]:
        minimum_tier = _minimum_proof_tier_for_claim(claim)
        if minimum_tier and tier_rank < PROOF_TIER_ORDER[minimum_tier]:
            errors.append(
                f"claimScope requestedClaim/allowedClaims require proofTier>={minimum_tier} for stronger acceptance claims"
            )
            break

    project_claim_seen = _claim_mentions_project_acceptance(requested_claim) or any(
        _claim_mentions_project_acceptance(claim) for claim in allowed_claims
    )
    if project_claim_seen and proof_tier != PROJECT_ACCEPTANCE_PROOF_TIER:
        errors.append("claimScope requestedClaim/allowedClaims cannot claim project acceptance/green unless proofTier=project_acceptance")

    normalized_forbidden = {_normalized_claim_text(claim) for claim in forbidden_claims if _normalized_claim_text(claim)}
    candidate_claims = [str(requested_claim or ""), *allowed_claims]
    for claim in candidate_claims:
        normalized_claim = _normalized_claim_text(claim)
        if not normalized_claim:
            continue
        for forbidden in normalized_forbidden:
            if normalized_claim == forbidden or forbidden in normalized_claim or normalized_claim in forbidden:
                errors.append("claimScope requestedClaim/allowedClaims contradict forbiddenClaims")
                return errors

    if proof_tier == PROJECT_ACCEPTANCE_PROOF_TIER:
        lifecycle = scope.get("lifecycleEvidence") if isinstance(scope.get("lifecycleEvidence"), dict) else {}
        project_refs = scope.get("projectEvidenceRefs")
        if (
            verdict != "PROVEN"
            or scope.get("projectAcceptanceEvidence") is not True
            or lifecycle.get("required") is not True
            or not lifecycle.get("refs")
            or not isinstance(project_refs, list)
            or not project_refs
        ):
            errors.append(
                "claimScope proofTier=project_acceptance requires verdict=PROVEN, projectAcceptanceEvidence=true, projectEvidenceRefs, and required lifecycle evidence refs"
            )
    return errors


def validate_claim_scope(issue: dict[str, Any], finalizer: dict[str, Any]) -> dict[str, Any]:
    scope = _claim_scope_object(finalizer)
    if scope is None:
        return {"ok": False, "reason": "PROCESS_ACCEPTANCE_FINALIZER_SCHEMA_BLOCKED: missing status.finalizer.claimScope/claim_scope object"}

    missing = [field for field in CLAIM_SCOPE_REQUIRED_FIELDS if field not in scope]
    if missing:
        return {"ok": False, "reason": "PROCESS_ACCEPTANCE_FINALIZER_SCHEMA_BLOCKED: claimScope missing required fields: " + ", ".join(missing)}

    errors: list[str] = []
    for field in ("subject", "requestedClaim"):
        if not isinstance(scope.get(field), str) or not scope.get(field, "").strip():
            errors.append(f"claimScope.{field} must be a non-empty string")
    subject = str(scope.get("subject") or "").strip()
    requested_claim = str(scope.get("requestedClaim") or "").strip()
    issue_id = str(issue.get("id") or "").strip()
    if issue_id and subject and subject != issue_id:
        errors.append("claimScope.subject must match issue id")

    proof_tier = str(scope.get("proofTier") or "").strip()
    verdict = str(scope.get("verdict") or "").strip().upper()
    if proof_tier not in CLAIM_SCOPE_PROOF_TIERS:
        errors.append("claimScope.proofTier is invalid")
    if verdict not in CLAIM_SCOPE_VERDICTS:
        errors.append("claimScope.verdict is invalid")
    if verdict == "PROVEN" and proof_tier in PROVEN_FORBIDDEN_PROOF_TIERS:
        errors.append(f"claimScope.verdict=PROVEN is not accepted for proofTier={proof_tier}")

    for field in ("allowedClaims", "forbiddenClaims", "limitations"):
        if not _non_empty_string_list(scope.get(field)):
            errors.append(f"claimScope.{field} must be a non-empty list")

    raw_refs, raw_ref_errors = _validate_raw_evidence_refs(issue, finalizer, scope)
    errors.extend(raw_ref_errors)
    errors.extend(_validate_required_ref_block(scope, "negativeControls"))
    errors.extend(_validate_required_ref_block(scope, "lifecycleEvidence"))
    errors.extend(_validate_review_route(issue, scope))
    errors.extend(_validate_project_claims(scope, proof_tier, verdict))

    finalizer_commits = _commit_values(finalizer)
    destination_ref = str(finalizer.get("destinationRef") or "").strip()
    scope_commits = _entry_commit_values(scope)
    if scope_commits and not any(commit in finalizer_commits for commit in scope_commits):
        errors.append("claimScope subjectCommit/subjectCommitOid must match status.finalizer commit")
    scope_destination_ref = _entry_destination_ref(scope)
    if scope_destination_ref and destination_ref and not _ref_matches(scope_destination_ref, destination_ref):
        errors.append("claimScope destinationRef must match status.finalizer destinationRef")

    expected_branch = str(issue.get("branch") or finalizer.get("branch") or "").strip()
    expected_worktree = str(issue.get("workspace") or finalizer.get("worktree") or finalizer.get("workspace") or "").strip()
    route = scope.get("reviewRoute") if isinstance(scope.get("reviewRoute"), dict) else {}
    expected_review_profile = _normalized_token(route.get("reviewProfile") or issue.get("reviewProfile") or (issue.get("meta") or {}).get("reviewProfile"))
    expected_review_tier = _normalized_token(route.get("reviewTier") or issue.get("reviewTier") or (issue.get("meta") or {}).get("reviewTier"))
    for index, ref in enumerate(_as_list(scope.get("reviewerEvidenceRefs"))):
        errors.extend(
            _entry_matches_claim_identity(
                ref,
                label="reviewerEvidenceRefs",
                index=index,
                subject=subject,
                requested_claim=requested_claim,
                finalizer_commits=finalizer_commits,
                destination_ref=destination_ref,
                expected_branch=expected_branch,
                expected_worktree=expected_worktree,
            )
        )
        errors.extend(
            _entry_review_route_errors(
                ref,
                label="reviewerEvidenceRefs",
                index=index,
                expected_profile=expected_review_profile,
                expected_tier=expected_review_tier,
            )
        )

    finalizer_identity = scope.get("finalizerIdentity")
    if not isinstance(finalizer_identity, dict):
        errors.append("claimScope.finalizerIdentity must be an object")
    else:
        for key in ("role", "sessionOrRunId", "artifactRef"):
            if not isinstance(finalizer_identity.get(key), str) or not finalizer_identity.get(key, "").strip():
                errors.append(f"claimScope.finalizerIdentity.{key} must be a non-empty string")
        artifact_ref = str(finalizer_identity.get("artifactRef") or "").strip()
        if artifact_ref:
            _path, err = normalize_exact_path(artifact_ref)
            if err:
                errors.append(f"claimScope.finalizerIdentity.artifactRef {err}")
        role = _normalized_token(finalizer_identity.get("role"))
        if role not in FINALIZER_IDENTITY_ROLES:
            errors.append("claimScope.finalizerIdentity.role must be mediator_finalizer, finalizer, or human")
        errors.extend(
            _entry_matches_claim_identity(
                finalizer_identity,
                label="finalizerIdentity",
                index=None,
                subject=subject,
                requested_claim=requested_claim,
                finalizer_commits=finalizer_commits,
                destination_ref=destination_ref,
                expected_branch=expected_branch,
                expected_worktree=expected_worktree,
            )
        )
        errors.extend(
            _entry_review_route_errors(
                finalizer_identity,
                label="finalizerIdentity",
                index=None,
                expected_profile=expected_review_profile,
                expected_tier=expected_review_tier,
            )
        )

    legacy_reason = _legacy_wiggum_reason(scope)
    if legacy_reason:
        errors.append(legacy_reason)
    raw_reason = _raw_diagnostic_failure_reason(scope)
    if raw_reason:
        errors.append(raw_reason)
    errors.extend(_raw_evidence_provenance_errors(scope, proof_tier, verdict))

    if errors:
        return {"ok": False, "reason": "; ".join(errors), "proofTier": proof_tier or None, "verdict": verdict or None}

    return {
        "ok": True,
        "reason": "claimScope accepted",
        "proofTier": proof_tier,
        "verdict": verdict,
        "allowedClaims": [str(item).strip() for item in scope.get("allowedClaims") if str(item).strip()],
        "forbiddenClaims": [str(item).strip() for item in scope.get("forbiddenClaims") if str(item).strip()],
        "limitations": [str(item).strip() for item in scope.get("limitations") if str(item).strip()],
        "rawEvidenceRefs": raw_refs,
        "reviewTier": _normalized_token((scope.get("reviewRoute") or {}).get("reviewTier")),
        "reviewProfile": _normalized_token((scope.get("reviewRoute") or {}).get("reviewProfile")),
    }


def _ref_matches(actual: str, expected: str) -> bool:
    if actual == expected:
        return True
    actual_branch = actual.removeprefix("refs/heads/")
    expected_branch = expected.removeprefix("refs/heads/")
    return actual_branch == expected_branch


def finalizer_evidence(
    issue: dict[str, Any],
    *,
    expected_subject_commit: str | None = None,
    expected_destination_ref: str | None = None,
    strict_done_lifecycle: bool = False,
    require_claim_scope: bool = True,
) -> dict[str, Any]:
    status = issue.get("status") if isinstance(issue.get("status"), dict) else {}
    finalizer = status.get("finalizer")
    if not isinstance(finalizer, dict):
        return {"ok": False, "status": "missing", "reason": "missing status.finalizer evidence"}

    forbidden_keys = forbidden_finalizer_evidence_keys(finalizer)
    if forbidden_keys:
        return {
            "ok": False,
            "status": "failed",
            "reason": "status.finalizer contains raw/log/diff/env/auth/secret/session/device-shaped metadata",
            "forbiddenEvidenceKeys": forbidden_keys[:20],
        }

    passed = finalizer.get("ok") is True or finalizer.get("passed") is True or str(finalizer.get("status") or "").strip().lower() in PASS_STATUSES
    if not passed:
        return {"ok": False, "status": "failed", "reason": "status.finalizer is not passed/ok"}
    if _non_empty_errors(finalizer.get("errors")):
        return {"ok": False, "status": "failed", "reason": "status.finalizer.errors is non-empty"}

    dest_ref = str(finalizer.get("destinationRef") or "").strip()
    if dest_ref == "local-only-human-review" and not isinstance(finalizer.get("localOnly"), dict):
        return {
            "ok": False,
            "status": "failed",
            "reason": "destinationRef=local-only-human-review requires a structured status.finalizer.localOnly object",
            "destinationRef": dest_ref,
        }

    commits = _commit_values(finalizer)
    if not commits:
        return {"ok": False, "status": "failed", "reason": "status.finalizer missing subjectCommit/subjectCommitOid"}
    if expected_subject_commit:
        expected = str(expected_subject_commit).strip()
        if expected and expected not in commits:
            return {
                "ok": False,
                "status": "failed",
                "reason": f"status.finalizer subject commit does not match expected commit {expected}",
                "subjectCommit": commits[0],
            }

    if expected_destination_ref:
        expected_ref = str(expected_destination_ref).strip()
        if expected_ref and not dest_ref:
            return {
                "ok": False,
                "status": "failed",
                "reason": f"status.finalizer missing destinationRef for expected {expected_ref}",
                "subjectCommit": commits[0],
            }
        if expected_ref and dest_ref and not _ref_matches(dest_ref, expected_ref):
            return {
                "ok": False,
                "status": "failed",
                "reason": f"status.finalizer destinationRef {dest_ref} does not match expected {expected_ref}",
                "subjectCommit": commits[0],
                "destinationRef": dest_ref,
            }

    if not any(key in finalizer for key in PATH_METADATA_KEYS):
        return {"ok": False, "status": "failed", "reason": "status.finalizer missing changed/staged path metadata", "subjectCommit": commits[0], "destinationRef": dest_ref or None}
    scope = finalizer_path_scope(issue, finalizer)
    if not scope.get("ok"):
        payload = {
            "ok": False,
            "status": "failed",
            "reason": scope.get("reason"),
            "subjectCommit": commits[0],
            "destinationRef": dest_ref or None,
        }
        if scope.get("offScopePaths"):
            payload["offScopePaths"] = scope.get("offScopePaths")
        if scope.get("controlPlanePaths"):
            payload["controlPlanePaths"] = scope.get("controlPlanePaths")
        return payload

    claim_scope = validate_claim_scope(issue, finalizer)
    if not claim_scope.get("ok"):
        return {
            "ok": False,
            "status": "failed",
            "reason": claim_scope.get("reason") or "claimScope invalid",
            **{k: v for k, v in claim_scope.items() if k in {"proofTier", "verdict"}},
            "subjectCommit": commits[0],
            "destinationRef": dest_ref or None,
        }

    if strict_done_lifecycle:
        lifecycle = done_lifecycle_evidence(finalizer)
        if not lifecycle.get("ok"):
            return {
                "ok": False,
                "status": lifecycle.get("status") or "NOT_DONE",
                "readyForHuman": bool(lifecycle.get("readyForHuman")),
                "reason": f"FINAL_REF_REQUIRED for Done: {lifecycle.get('reason')}",
                "subjectCommit": commits[0],
                "destinationRef": dest_ref or None,
                **{k: v for k, v in lifecycle.items() if k not in {"ok", "status", "reason", "readyForHuman"}},
            }

    return {
        "ok": True,
        "status": "passed",
        "reason": "status.finalizer evidence passed",
        "subjectCommit": commits[0],
        "destinationRef": dest_ref or None,
        "changedPaths": normalize_path_list(_as_list(finalizer.get("changedPaths")))[0],
        "stagedPaths": normalize_path_list(_as_list(finalizer.get("stagedPaths")))[0],
        **{k: v for k, v in claim_scope.items() if k not in {"ok", "reason"}},
    }


def finalizer_transition_guard(
    issue: dict[str, Any],
    transition: str = "completion",
    *,
    expected_subject_commit: str | None = None,
    expected_destination_ref: str | None = None,
    strict_done_lifecycle: bool | None = None,
) -> dict[str, Any]:
    if strict_done_lifecycle is None:
        strict_done_lifecycle = transition == "Done"
    required, requirement_reason = issue_requires_finalizer(issue)
    explicit_not_required = _explicit_finalizer_not_required(issue)
    local = local_only_exception(issue, strict_completion=transition != "render")
    base: dict[str, Any] = {
        "issueId": issue.get("id"),
        "transition": transition,
        "required": required,
        "requirementReason": requirement_reason,
        "localOnly": local,
    }
    status = issue.get("status") if isinstance(issue.get("status"), dict) else {}
    finalizer = status.get("finalizer") if isinstance(status.get("finalizer"), dict) else {}
    forbidden_keys = forbidden_finalizer_evidence_keys(finalizer)
    if forbidden_keys:
        return {
            **base,
            "ok": False,
            "status": "failed",
            "required": required,
            "reason": f"FINALIZER_REQUIRED for {transition}: status.finalizer contains raw/log/diff/env/auth/secret/session/device-shaped metadata",
            "forbiddenEvidenceKeys": forbidden_keys[:20],
        }

    finalizer_metadata_present = bool(finalizer)
    scope = finalizer_path_scope(
        issue,
        finalizer,
        reject_control_plane=(bool(local.get("present")) or not required or explicit_not_required),
    )
    if finalizer_metadata_present and not scope.get("ok"):
        payload = {
            **base,
            "ok": False,
            "status": "failed",
            "required": required,
            "reason": f"FINALIZER_REQUIRED for {transition}: {scope.get('reason')}",
        }
        if scope.get("offScopePaths"):
            payload["offScopePaths"] = scope.get("offScopePaths")
        if scope.get("controlPlanePaths"):
            payload["controlPlanePaths"] = scope.get("controlPlanePaths")
        return payload
    if local.get("present") and not local.get("ok"):
        return {
            **base,
            "ok": False,
            "status": "failed",
            "required": required,
            "reason": f"FINALIZER_REQUIRED for {transition}: invalid localOnly exception: {local.get('reason')}",
        }
    if not required:
        return {**base, "ok": True, "status": "not_required", "reason": requirement_reason}
    if not scope.get("ok"):
        payload = {
            **base,
            "ok": False,
            "status": "failed",
            "required": required,
            "reason": f"FINALIZER_REQUIRED for {transition}: {scope.get('reason')}",
        }
        if scope.get("offScopePaths"):
            payload["offScopePaths"] = scope.get("offScopePaths")
        if scope.get("controlPlanePaths"):
            payload["controlPlanePaths"] = scope.get("controlPlanePaths")
        return payload
    if local.get("ok") is True:
        transition_token = _normalized_token(transition)
        if (transition == "Done" or transition_token in READY_COMPLETION_TRANSITIONS) and not local.get("finalDeliverable"):
            return {
                **base,
                "ok": False,
                "status": "NOT_DONE",
                "readyForHuman": True,
                "reason": f"FINALIZER_REQUIRED for {transition}: localOnly is a human-review/local candidate and not an explicit final deliverable",
                "doneStatus": local.get("doneStatus") or "NOT_DONE",
                "completionMode": local.get("completionMode"),
            }
        return {
            **base,
            "ok": True,
            "status": "localOnly",
            "reason": "strict localOnly finalizer exception accepted",
            "doneStatus": local.get("doneStatus"),
            "completionMode": local.get("completionMode"),
        }

    evidence = finalizer_evidence(
        issue,
        expected_subject_commit=expected_subject_commit,
        expected_destination_ref=expected_destination_ref,
        strict_done_lifecycle=strict_done_lifecycle,
        require_claim_scope=True,
    )
    if evidence.get("ok"):
        return {**base, **evidence, "ok": True, "required": True}

    reason = evidence.get("reason") or "required finalizer evidence missing"
    if local.get("present") and not local.get("ok"):
        reason = f"{reason}; invalid localOnly exception: {local.get('reason')}"
    return {**base, **evidence, "ok": False, "required": True, "reason": f"FINALIZER_REQUIRED for {transition}: {reason}"}


def render_status(issue: dict[str, Any]) -> dict[str, Any]:
    transition = "Done" if str(issue.get("state") or "") == "Done" else "render"
    result = finalizer_transition_guard(issue, transition)
    rendered = {
        "required": bool(result.get("required")),
        "ok": bool(result.get("ok")),
        "status": result.get("status"),
        "reason": result.get("reason"),
    }
    if result.get("subjectCommit"):
        rendered["subjectCommit"] = result.get("subjectCommit")
    if result.get("destinationRef"):
        rendered["destinationRef"] = result.get("destinationRef")
    for key in (
        "proofTier",
        "verdict",
        "allowedClaims",
        "forbiddenClaims",
        "limitations",
        "doneStatus",
        "completionMode",
        "readyForHuman",
        "reviewTier",
        "reviewProfile",
    ):
        if result.get(key) not in (None, ""):
            rendered[key] = result.get(key)
    local = result.get("localOnly") if isinstance(result.get("localOnly"), dict) else {}
    if local.get("present"):
        rendered["localOnly"] = {
            "ok": bool(local.get("ok")),
            "paths": local.get("paths") or [],
            "rationale": local.get("rationale") or "",
            "reason": local.get("reason"),
            "completionMode": local.get("completionMode") or "",
            "doneStatus": local.get("doneStatus") or "NOT_DONE",
        }
    for key in ("changedPaths", "stagedPaths"):
        if result.get(key) is not None:
            rendered[key] = result.get(key)
    return rendered


def finalizer_summary(issue: dict[str, Any]) -> str | None:
    info = render_status(issue)
    status = info.get("status")
    if status == "not_required" and not info.get("required"):
        return None
    if status == "localOnly" or (status == "NOT_DONE" and (info.get("localOnly") or {}).get("ok")):
        local = info.get("localOnly") or {}
        paths = ",".join(local.get("paths") or []) or "(none)"
        rationale = local.get("rationale") or "(missing rationale)"
        done_status = local.get("doneStatus") or info.get("doneStatus") or "NOT_DONE"
        completion_mode = local.get("completionMode") or info.get("completionMode") or ""
        mode_part = f" completionMode={completion_mode}" if completion_mode else ""
        return f"finalizer=localOnly paths={paths} rationale={rationale}{mode_part} status={done_status}"
    if info.get("ok"):
        commit = str(info.get("subjectCommit") or "")[:12]
        dest = info.get("destinationRef")
        verdict = info.get("verdict")
        proof_tier = info.get("proofTier")
        parts = [f"finalizer={verdict}" if verdict else "finalizer=passed"]
        if proof_tier:
            parts.append(f"proofTier={proof_tier}")
        limitations = info.get("limitations") if isinstance(info.get("limitations"), list) else []
        if limitations:
            parts.append("limitations=" + ",".join(str(item).replace(" ", "_") for item in limitations[:3]))
        done_status = info.get("doneStatus")
        if done_status:
            parts.append(f"status={done_status}")
        if commit:
            parts.append(f"commit={commit}")
        if dest:
            parts.append(f"dest={dest}")
        return " ".join(parts)
    return f"finalizer={status or 'missing'} reason={info.get('reason')}"


def load_issues(root: Path) -> dict[str, dict[str, Any]]:
    issues_dir = root / "state" / "issues"
    out: dict[str, dict[str, Any]] = {}
    if not issues_dir.exists():
        return out
    for path in sorted(issues_dir.glob("*.json")):
        try:
            issue = json.loads(path.read_text())
        except Exception:
            issue = {"id": path.stem, "state": "Blocked", "status": {"blockedReason": "unreadable issue json"}}
        if isinstance(issue, dict):
            out[str(issue.get("id") or path.stem)] = issue
    return out


def epic_child_ids(epic_id: str, epic: dict[str, Any], issues_by_id: dict[str, dict[str, Any]], *, strict_declared_children: bool = False, allowed_child_ids: set[str] | None = None) -> set[str]:
    child_ids = {str(cid) for cid in (epic.get("children") or []) if str(cid)}
    if not strict_declared_children:
        child_ids.update(cid for cid, child in issues_by_id.items() if child.get("parent") == epic_id and child.get("kind") != "epic")
    if allowed_child_ids is not None:
        child_ids = {cid for cid in child_ids if cid in allowed_child_ids}
    return child_ids


def required_child_finalizer_failures(
    epic_id: str,
    epic: dict[str, Any],
    issues_by_id: dict[str, dict[str, Any]],
    *,
    strict_declared_children: bool = False,
    allowed_child_ids: set[str] | None = None,
    complete_states: set[str] | None = None,
) -> list[dict[str, Any]]:
    complete_states = complete_states or {"Done"}
    failures: list[dict[str, Any]] = []
    for child_id in sorted(epic_child_ids(epic_id, epic, issues_by_id, strict_declared_children=strict_declared_children, allowed_child_ids=allowed_child_ids)):
        child = issues_by_id.get(child_id)
        if not child or not is_required_issue(child):
            continue
        if str(child.get("state") or "") not in complete_states:
            continue
        child_state = str(child.get("state") or "")
        result = finalizer_transition_guard(
            child,
            "Done" if child_state == "Done" else "epic auto-advance",
            strict_done_lifecycle=True if child_state == "Done" else None,
        )
        if result.get("required") and not result.get("ok"):
            failures.append(result)
    return failures


def authorized_scope_issue_ids(
    orch: dict[str, Any],
    issues_by_id: dict[str, dict[str, Any]],
    *,
    frozen_window: bool = False,
    approved_epic_ids: set[str] | None = None,
    approved_issue_ids: set[str] | None = None,
) -> set[str]:
    approved_epic_ids = approved_epic_ids if approved_epic_ids is not None else {str(v) for v in (orch.get("approvedEpicIds") or []) if str(v)}
    approved_issue_ids = approved_issue_ids if approved_issue_ids is not None else {str(v) for v in (orch.get("approvedIssueIds") or []) if str(v)}
    checked: set[str] = set(approved_issue_ids)
    if frozen_window:
        for epic_id in approved_epic_ids:
            epic = issues_by_id.get(epic_id)
            if epic:
                checked.update(epic_child_ids(epic_id, epic, issues_by_id, strict_declared_children=True))
        return checked
    authorized_epic = str(orch.get("authorizedEpic") or "")
    if authorized_epic:
        epic = issues_by_id.get(authorized_epic)
        if epic:
            checked.update(epic_child_ids(authorized_epic, epic, issues_by_id))
    for epic_id in approved_epic_ids:
        epic = issues_by_id.get(epic_id)
        if epic:
            checked.update(epic_child_ids(epic_id, epic, issues_by_id))
    return checked


def authorized_scope_finalizer_failures(
    orch: dict[str, Any],
    issues_by_id: dict[str, dict[str, Any]],
    *,
    frozen_window: bool = False,
    approved_epic_ids: set[str] | None = None,
    approved_issue_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    approved_epic_ids = approved_epic_ids if approved_epic_ids is not None else {str(v) for v in (orch.get("approvedEpicIds") or []) if str(v)}
    checked_ids = authorized_scope_issue_ids(
        orch,
        issues_by_id,
        frozen_window=frozen_window,
        approved_epic_ids=approved_epic_ids,
        approved_issue_ids=approved_issue_ids,
    )
    authorized_epic = str(orch.get("authorizedEpic") or "").strip()
    if authorized_epic:
        checked_ids.add(authorized_epic)
    checked_ids.update(approved_epic_ids)
    for issue_id in sorted(checked_ids):
        issue = issues_by_id.get(issue_id)
        if not issue or str(issue.get("state") or "") not in COMPLETE_STATES_REQUIRING_FINALIZER:
            continue
        issue_state = str(issue.get("state") or "")
        result = finalizer_transition_guard(
            issue,
            "Done" if issue_state == "Done" else "READY reconciliation",
            strict_done_lifecycle=True if issue_state == "Done" else None,
        )
        if result.get("required") and not result.get("ok"):
            failures.append(result)
    return failures


def failure_message(failures: list[dict[str, Any]], *, prefix: str = "FINALIZER_REQUIRED") -> str:
    parts = []
    for failure in failures[:5]:
        iid = failure.get("issueId") or "(unknown)"
        parts.append(f"{iid}: {failure.get('reason')}")
    extra = "" if len(failures) <= 5 else f"; +{len(failures) - 5} more"
    return f"{prefix}: " + "; ".join(parts) + extra


def _load_json_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate required issue finalizer evidence")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]), help="workspace root containing state/issues")
    parser.add_argument("--issue-id")
    parser.add_argument("--issue-file")
    parser.add_argument("--orchestrator-file")
    parser.add_argument("--check-authorized-scope", action="store_true")
    parser.add_argument("--frozen-window", action="store_true")
    parser.add_argument("--approved-epic-id", action="append", default=[])
    parser.add_argument("--approved-issue-id", action="append", default=[])
    parser.add_argument("--transition", default="completion")
    parser.add_argument("--subject-commit")
    parser.add_argument("--destination-ref")
    parser.add_argument(
        "--strict-done-lifecycle",
        action="store_true",
        help="require branch/PR/default-ref publication evidence before a Done transition",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if args.check_authorized_scope:
        orch_path = Path(args.orchestrator_file) if args.orchestrator_file else root / "state" / "orchestrator.json"
        orch = _load_json_file(orch_path) if orch_path.exists() else {}
        issues = load_issues(root)
        approved_issue_ids = {str(v) for v in (args.approved_issue_id or []) if str(v)}
        if not approved_issue_ids:
            approved_issue_ids = {str(v) for v in (orch.get("approvedIssueIds") or []) if str(v)}
        approved_epic_ids = {str(v) for v in (args.approved_epic_id or []) if str(v)}
        if not approved_epic_ids:
            approved_epic_ids = {str(v) for v in (orch.get("approvedEpicIds") or []) if str(v)}
        failures = authorized_scope_finalizer_failures(
            orch,
            issues,
            frozen_window=bool(args.frozen_window or orch.get("authorizationFrozenAt")),
            approved_epic_ids=approved_epic_ids,
            approved_issue_ids=approved_issue_ids,
        )
        result = {"ok": not failures, "failures": failures}
        if failures:
            result["reason"] = failure_message(failures)
        if not args.quiet or failures:
            print(json.dumps(result, indent=2))
        return 0 if not failures else 1

    if args.issue_file:
        issue = _load_json_file(Path(args.issue_file))
    elif args.issue_id:
        issue = _load_json_file(root / "state" / "issues" / f"{args.issue_id}.json")
    else:
        parser.error("one of --issue-file, --issue-id, or --check-authorized-scope is required")

    result = finalizer_transition_guard(
        issue,
        args.transition,
        expected_subject_commit=args.subject_commit,
        expected_destination_ref=args.destination_ref,
        strict_done_lifecycle=args.strict_done_lifecycle,
    )
    if not args.quiet or not result.get("ok"):
        print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
