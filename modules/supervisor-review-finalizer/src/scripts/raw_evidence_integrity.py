"""Shared fail-closed validation for raw evidence references."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from pathlib import Path
from typing import Any, Callable, Iterable

SHA256_RE = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")

ACCEPTED_VALIDATION_STATUSES = {
    "accepted",
    "ok",
    "pass",
    "passed",
    "success",
    "succeeded",
    "valid",
    "validated",
}
SOURCE_OR_EXIT_KINDS = {"source_hash", "exit_code"}
PROVENANCE_KEYS = (
    "runId",
    "runIdentity",
    "run_identity",
    "commandId",
    "command_id",
    "source",
    "sourceRef",
    "source_ref",
    "sourceHash",
    "source_hash",
    "sourceSha256",
    "source_sha256",
    "sourceAuthority",
    "sourceAuthorityRef",
    "source_authority",
    "source_authority_ref",
)
NEGATIVE_VALIDATION_FLAGS = (
    "failedRequiredReads",
    "failed_required_reads",
    "stderrContradictions",
    "stderr_contradictions",
    "nonzeroExitsNormalizedAway",
    "nonzero_exits_normalized_away",
    "requiredReadsNormalizedAway",
    "required_reads_normalized_away",
)
POSITIVE_VALIDATION_FLAGS = (
    "noFailedRequiredReads",
    "no_failed_required_reads",
    "rawDiagnosticsPreserved",
    "raw_diagnostics_preserved",
)
RAW_FAILURE_MARKERS = (
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
RAW_JSON_FAILURE_FIELDS = {"status", "verdict", "result", "conclusion", "outcome"}
RAW_JSON_FAILURE_VALUES = {"failed", "failure", "fail", "error", "errored", "blocked", "nonzero", "non_zero"}
RAW_JSON_EXIT_FIELDS = {"exitcode", "exit_code", "returncode", "return_code", "code"}
RAW_JSON_FALSE_FIELDS = {"ok", "success"}


def _normalize_token(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def strip_sha256_prefix(value: Any) -> str:
    return str(value or "").removeprefix("sha256:").strip().lower()


def normalize_relative_artifact_path(raw: Any) -> tuple[str | None, str | None]:
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
        return None, "path resolves to artifact root"
    if normalized == ".." or normalized.startswith("../") or "/../" in f"/{normalized}/":
        return None, f"path escapes artifact root: {text}"
    if normalized.endswith("/"):
        return None, f"path is not exact: {text}"
    return normalized, None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def raw_ref_has_provenance(ref: Any) -> bool:
    if not isinstance(ref, dict):
        return False
    if _normalize_token(ref.get("kind")) in SOURCE_OR_EXIT_KINDS:
        return True
    if any(str(ref.get(key) or "").strip() for key in PROVENANCE_KEYS):
        return True
    for key in ("exitCode", "exit_code", "returnCode", "return_code"):
        value = ref.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return True
        if isinstance(value, str) and value.strip():
            return True
    return False


def _validation_object(ref: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("validation", "validationResult", "structuredValidationResult"):
        value = ref.get(key)
        if isinstance(value, dict):
            return value
    return None


def _negative_flag_present(value: Any) -> bool:
    if value in (False, 0, None, "", [], {}):
        return False
    if isinstance(value, str):
        return _normalize_token(value) not in {"false", "no", "none", "null", "0", "absent", "empty"}
    return True


def _positive_flag_present(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return _normalize_token(value) in {"true", "yes", "1", "ok", "pass", "passed", "accepted", "preserved"}
    return False


def structured_validation_errors(ref: dict[str, Any], label: str) -> list[str]:
    validation = _validation_object(ref)
    if validation is None:
        return [f"{label}.validation must be a structured object"]

    errors: list[str] = []
    status = _normalize_token(validation.get("status") or validation.get("validationStatus"))
    if status not in ACCEPTED_VALIDATION_STATUSES:
        errors.append(f"{label}.validation.status must be accepted/pass/ok")
    for key in NEGATIVE_VALIDATION_FLAGS:
        if _negative_flag_present(validation.get(key)):
            errors.append(f"{label}.validation.{key} must be absent/false/empty")
    if not any(_positive_flag_present(validation.get(key)) for key in POSITIVE_VALIDATION_FLAGS):
        errors.append(f"{label}.validation must positively confirm noFailedRequiredReads or rawDiagnosticsPreserved")
    return errors


def _raw_json_failure_reasons(value: Any, location: str = "$") -> list[str]:
    reasons: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            normalized_key = _normalize_token(key)
            child_location = f"{location}.{key_text}" if location else key_text
            if normalized_key in RAW_JSON_FAILURE_FIELDS and isinstance(child, str):
                normalized_value = _normalize_token(child)
                if normalized_value in RAW_JSON_FAILURE_VALUES:
                    reasons.append(f"{child_location}={child!r} declares failure")
            if normalized_key in RAW_JSON_EXIT_FIELDS and isinstance(child, int) and not isinstance(child, bool) and child != 0:
                reasons.append(f"{child_location}={child} is nonzero")
            if normalized_key in RAW_JSON_FALSE_FIELDS and child is False:
                reasons.append(f"{child_location} is false")
            reasons.extend(_raw_json_failure_reasons(child, child_location))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reasons.extend(_raw_json_failure_reasons(child, f"{location}[{index}]"))
    return reasons


def _content_failure_errors(data: bytes, label: str) -> list[str]:
    errors: list[str] = []
    text = data[:1_000_000].decode("utf-8", errors="replace")
    if any(marker in text.lower() for marker in RAW_FAILURE_MARKERS):
        errors.append(f"RAW_EVIDENCE_BLOCKED: {label} contains failed required diagnostics")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return errors
    json_reasons = _raw_json_failure_reasons(parsed)
    if json_reasons:
        errors.append(
            f"RAW_EVIDENCE_BLOCKED: {label} structured JSON reports failure: "
            + "; ".join(json_reasons[:5])
        )
    return errors


def validate_raw_evidence_refs(
    refs: Any,
    *,
    artifact_root: Path,
    label: str,
    allowed_kinds: Iterable[str],
    legacy_reason: Callable[[Any], str | None] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate raw evidence refs against local artifacts and structured metadata."""

    if not isinstance(refs, list) or not refs:
        return [], [f"{label} must be a non-empty list"]

    allowed_kind_set = {str(kind) for kind in allowed_kinds}
    root = artifact_root.expanduser().resolve()
    validated: list[dict[str, Any]] = []
    errors: list[str] = []

    for index, ref in enumerate(refs):
        item_label = f"{label}[{index}]"
        before = len(errors)
        if not isinstance(ref, dict):
            errors.append(f"{item_label} must be an object")
            continue

        if legacy_reason:
            reason = legacy_reason(ref)
            if reason:
                errors.append(f"{item_label}: {reason}")
                continue

        normalized_path, path_error = normalize_relative_artifact_path(ref.get("path"))
        if path_error:
            errors.append(f"{item_label}.path {path_error}")
            continue
        assert normalized_path is not None

        kind = str(ref.get("kind") or "").strip()
        if kind not in allowed_kind_set:
            errors.append(f"{item_label}.kind must be one of {sorted(allowed_kind_set)}")

        raw_sha = str(ref.get("sha256") or "").strip()
        expected_sha = strip_sha256_prefix(raw_sha)
        if not raw_sha:
            errors.append(f"{item_label}.sha256 is required")
        elif not SHA256_RE.fullmatch(raw_sha):
            errors.append(f"{item_label}.sha256 must be sha256:<64 hex> or 64 hex")

        if not raw_ref_has_provenance(ref):
            errors.append(f"{item_label} must carry run/source/command/exit provenance")

        errors.extend(structured_validation_errors(ref, item_label))

        artifact = (root / normalized_path).resolve()
        try:
            artifact.relative_to(root)
        except ValueError:
            errors.append(f"{item_label}.path escapes artifact root")
            continue
        if not artifact.is_file():
            errors.append(f"{item_label}.path does not exist in artifact root: {normalized_path}")
            continue

        try:
            data = artifact.read_bytes()
        except OSError as exc:
            errors.append(f"{item_label}.path could not be read for raw validation: {exc}")
            continue

        actual_sha = hashlib.sha256(data).hexdigest()
        if expected_sha and actual_sha != expected_sha:
            errors.append(f"{item_label}.sha256 does not match referenced local artifact")
        errors.extend(_content_failure_errors(data, item_label))

        if len(errors) == before:
            validated.append({"path": normalized_path, "kind": kind, "sha256": actual_sha})

    return validated, errors
