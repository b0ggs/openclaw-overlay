#!/usr/bin/env python3
"""Claim-scope acceptance-authority regressions for the finalizer guard."""

from __future__ import annotations

import sys
import atexit
import hashlib
import json
import shutil
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import finalizer_required  # noqa: E402
from harness.path_config import openclaw_home  # noqa: E402

RAW_EVIDENCE_ROOT = Path(tempfile.mkdtemp(prefix="openclaw-finalizer-raw-evidence-"))
atexit.register(lambda: shutil.rmtree(RAW_EVIDENCE_ROOT, ignore_errors=True))
OPENCLAW_HOME = openclaw_home()


def accepted_raw_validation() -> dict[str, Any]:
    return {
        "status": "accepted",
        "noFailedRequiredReads": True,
        "rawDiagnosticsPreserved": True,
        "failedRequiredReads": False,
        "stderrContradictions": False,
        "nonzeroExitsNormalizedAway": False,
        "requiredReadsNormalizedAway": False,
    }


def write_raw_fixture(relpath: str, content: str) -> str:
    path = RAW_EVIDENCE_ROOT / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def structured_raw_evidence_refs() -> list[dict[str, Any]]:
    raw_sha = write_raw_fixture("evidence/raw-command.json", json.dumps({"status": "ok", "exitCode": 0}) + "\n")
    source_sha = write_raw_fixture("evidence/source.hash", "source-hash-ok\n")
    exit_sha = write_raw_fixture("evidence/exit-code.json", json.dumps({"exitCode": 0}) + "\n")
    return [
        {
            "path": "evidence/raw-command.json",
            "kind": "validator_json",
            "runId": "run-123",
            "sha256": raw_sha,
            "validation": accepted_raw_validation(),
        },
        {
            "path": "evidence/source.hash",
            "kind": "source_hash",
            "sha256": source_sha,
            "validation": accepted_raw_validation(),
        },
        {
            "path": "evidence/exit-code.json",
            "kind": "exit_code",
            "exitCode": 0,
            "sha256": exit_sha,
            "validation": accepted_raw_validation(),
        },
    ]


def claim_scope(**updates: Any) -> dict[str, Any]:
    scope: dict[str, Any] = {
        "subject": "fwk-process-guard",
        "requestedClaim": "generic process guard candidate is ready for scoped review",
        "proofTier": "runtime_parity",
        "verdict": "PROVEN",
        "subjectCommitOid": "abc123",
        "destinationRef": "refs/heads/main",
        "worktree": str(RAW_EVIDENCE_ROOT),
        "rawEvidenceRefs": structured_raw_evidence_refs(),
        "negativeControls": {
            "required": True,
            "refs": [{"path": "evidence/negative-control.json"}],
            "notApplicableReason": "",
        },
        "lifecycleEvidence": {
            "required": True,
            "refs": [{"path": "evidence/lifecycle.log"}],
            "notApplicableReason": "",
        },
        "allowedClaims": [
            "generic process guard candidate is ready for scoped review",
            "candidate guard enforces the named process invariant",
        ],
        "forbiddenClaims": ["project acceptance", "Overlay module acceptance"],
        "limitations": ["local candidate only; not landed on default ref"],
        "stateAuthorityRefs": [{"path": "state/issues/fwk-process-guard.json"}],
        "reviewRoute": {
            "reviewProfile": "ops_pipeline",
            "reviewTier": "primary_checker_mediator",
            "requiredRoles": ["primary", "checker", "mediator"],
            "completedRoles": ["primary", "checker", "mediator"],
            "orchestratorCountedAsReviewer": False,
        },
        "reviewerEvidenceRefs": [
            {
                "path": "reviews/primary.json",
                "subject": "fwk-process-guard",
                "requestedClaim": "generic process guard candidate is ready for scoped review",
                "subjectCommitOid": "abc123",
                "destinationRef": "refs/heads/main",
                "reviewRoute": {"reviewProfile": "ops_pipeline", "reviewTier": "primary_checker_mediator"},
            },
            {
                "path": "reviews/checker.json",
                "subject": "fwk-process-guard",
                "requestedClaim": "generic process guard candidate is ready for scoped review",
                "subjectCommitOid": "abc123",
                "destinationRef": "refs/heads/main",
                "reviewRoute": {"reviewProfile": "ops_pipeline", "reviewTier": "primary_checker_mediator"},
            },
            {
                "path": "reviews/mediator.json",
                "subject": "fwk-process-guard",
                "requestedClaim": "generic process guard candidate is ready for scoped review",
                "subjectCommitOid": "abc123",
                "destinationRef": "refs/heads/main",
                "reviewRoute": {"reviewProfile": "ops_pipeline", "reviewTier": "primary_checker_mediator"},
            },
        ],
        "finalizerIdentity": {
            "role": "mediator_finalizer",
            "sessionOrRunId": "run-123",
            "artifactRef": "reviews/mediator.json",
            "subject": "fwk-process-guard",
            "requestedClaim": "generic process guard candidate is ready for scoped review",
            "subjectCommitOid": "abc123",
            "destinationRef": "refs/heads/main",
            "reviewRoute": {"reviewProfile": "ops_pipeline", "reviewTier": "primary_checker_mediator"},
        },
    }
    scope.update(updates)
    subject = str(scope.get("subject") or "")
    requested = str(scope.get("requestedClaim") or "")
    commit = str(scope.get("subjectCommitOid") or scope.get("subjectCommit") or "")
    dest = str(scope.get("destinationRef") or "")
    if "reviewerEvidenceRefs" not in updates:
        for ref in scope["reviewerEvidenceRefs"]:
            ref["subject"] = subject
            ref["requestedClaim"] = requested
            ref["subjectCommitOid"] = commit
            ref["destinationRef"] = dest
            ref["reviewRoute"] = {"reviewProfile": "ops_pipeline", "reviewTier": "primary_checker_mediator"}
    if "finalizerIdentity" not in updates:
        scope["finalizerIdentity"]["subject"] = subject
        scope["finalizerIdentity"]["requestedClaim"] = requested
        scope["finalizerIdentity"]["subjectCommitOid"] = commit
        scope["finalizerIdentity"]["destinationRef"] = dest
        scope["finalizerIdentity"]["reviewRoute"] = {"reviewProfile": "ops_pipeline", "reviewTier": "primary_checker_mediator"}
    return scope


def finalizer_with_scope(scope: dict[str, Any] | None = None, **updates: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "status": "passed",
        "subjectCommitOid": "abc123",
        "destinationRef": "refs/heads/main",
        "changedPaths": ["scripts/finalizer_required.py"],
        "stagedPaths": ["scripts/finalizer_required.py"],
        "errors": [],
    }
    if scope is not None:
        payload["claimScope"] = scope
    payload.update(updates)
    return payload


def issue_with_finalizer(finalizer: dict[str, Any], **updates: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "fwk-process-guard",
        "project": "(framework)",
        "state": "Human Review",
        "finalizerRequired": True,
        "allowedPaths": ["scripts/finalizer_required.py"],
        "status": {"finalizer": finalizer},
        "reviewProfile": "ops_pipeline",
        "reviewTier": "primary_checker_mediator",
    }
    payload.update(updates)
    return payload


class ClaimScopeFinalizerTests(unittest.TestCase):
    def guard(self, finalizer: dict[str, Any], transition: str = "Human Review", **issue_updates: Any) -> dict[str, Any]:
        return finalizer_required.finalizer_transition_guard(issue_with_finalizer(finalizer, **issue_updates), transition)

    def test_passing_finalizer_without_claim_scope_blocks_human_review(self) -> None:
        result = self.guard(finalizer_with_scope(None), "Human Review")

        self.assertFalse(result["ok"])
        self.assertIn("claimScope", result["reason"])

    def test_passing_finalizer_without_claim_scope_blocks_done(self) -> None:
        result = self.guard(finalizer_with_scope(None), "Done")

        self.assertFalse(result["ok"])
        self.assertIn("claimScope", result["reason"])

    def test_candidate_proof_tier_cannot_claim_proven(self) -> None:
        result = self.guard(finalizer_with_scope(claim_scope(proofTier="candidate", verdict="PROVEN")))

        self.assertFalse(result["ok"])
        self.assertIn("verdict=PROVEN is not accepted for proofTier=candidate", result["reason"])

    def test_allowed_forbidden_and_limitations_must_be_non_empty(self) -> None:
        for field in ("allowedClaims", "forbiddenClaims", "limitations"):
            with self.subTest(field=field):
                scope = claim_scope(**{field: []})
                result = self.guard(finalizer_with_scope(scope))

                self.assertFalse(result["ok"])
                self.assertIn(f"claimScope.{field} must be a non-empty list", result["reason"])

    def test_local_only_human_review_destination_without_structured_local_only_blocks(self) -> None:
        result = self.guard(
            finalizer_with_scope(
                claim_scope(),
                destinationRef="local-only-human-review",
            )
        )

        self.assertFalse(result["ok"])
        self.assertIn("local-only-human-review requires a structured", result["reason"])

    def test_local_only_rejects_control_plane_derived_private_memory_and_broad_evidence_paths(self) -> None:
        cases = {
            "control-plane": "scripts/finalizer_required.py",
            "derived-state": "state/active-tasks.json",
            "private-memory": "MEMORY.md",
            "broad-evidence-tree": "handoffs/process-evidence-root",
        }
        for label, path in cases.items():
            with self.subTest(label=label):
                issue = {
                    "id": f"local-{label}",
                    "project": "(framework)",
                    "state": "Human Review",
                    "allowedPaths": [path],
                    "status": {
                        "finalizer": {
                            "localOnly": {
                                "paths": [path],
                                "rationale": "local-only candidate for human review",
                                "completionMode": "local-only",
                                "doneStatus": "NOT_DONE",
                            }
                        }
                    },
                }

                result = finalizer_required.finalizer_transition_guard(issue, "Human Review")

                self.assertFalse(result["ok"], result)

    def test_primary_checker_mediator_requires_mediator_role(self) -> None:
        scope = claim_scope()
        scope["reviewRoute"] = deepcopy(scope["reviewRoute"])
        scope["reviewRoute"]["completedRoles"] = ["primary", "checker"]

        result = self.guard(finalizer_with_scope(scope))

        self.assertFalse(result["ok"])
        self.assertIn("completedRoles must include primary, checker, mediator", result["reason"])

    def test_ops_pipeline_cannot_use_alpha_beta_only_evidence(self) -> None:
        scope = claim_scope()
        scope["reviewRoute"] = deepcopy(scope["reviewRoute"])
        scope["reviewRoute"]["completedRoles"] = ["alpha", "alpha-prime", "beta"]

        result = self.guard(finalizer_with_scope(scope))

        self.assertFalse(result["ok"])
        self.assertIn("ops_pipeline cannot be satisfied by Alpha/Beta/security-only evidence", result["reason"])

    def test_stale_carrier_override_cannot_suppress_required_review_roles(self) -> None:
        scope = claim_scope()
        scope["reviewRoute"] = deepcopy(scope["reviewRoute"])
        scope["reviewRoute"]["completedRoles"] = ["primary", "checker"]
        issue_carrier = {
            "carrierOverride": {
                "enabled": True,
                "mode": "main_session_only_no_subagents",
                "reason": "stale old wave",
            }
        }

        result = self.guard(finalizer_with_scope(scope), carrierOverride=issue_carrier["carrierOverride"])

        self.assertFalse(result["ok"])
        self.assertIn("carrierOverride/no_subagents cannot suppress required review roles", result["reason"])

    def test_renderers_surface_scoped_verdict_and_limitations_without_plain_pass(self) -> None:
        scoped = claim_scope(
            proofTier="candidate",
            verdict="CANDIDATE",
            limitations=["candidate only", "not default-ref landed"],
        )
        issue = issue_with_finalizer(finalizer_with_scope(scoped))

        rendered = finalizer_required.render_status(issue)
        summary = finalizer_required.finalizer_summary(issue)

        self.assertTrue(rendered["ok"], rendered)
        self.assertEqual(rendered["proofTier"], "candidate")
        self.assertEqual(rendered["verdict"], "CANDIDATE")
        self.assertIn("candidate only", rendered["limitations"])
        self.assertIsNotNone(summary)
        self.assertIn("finalizer=CANDIDATE", summary or "")
        self.assertIn("proofTier=candidate", summary or "")
        self.assertNotIn("finalizer=passed", summary or "")

    def test_wiggum_legacy_state_cannot_count_as_acceptance_evidence(self) -> None:
        scope = claim_scope(rawEvidenceRefs=[{"path": str(OPENCLAW_HOME / "active-tasks.json"), "kind": "validator_json"}])

        result = self.guard(finalizer_with_scope(scope))

        self.assertFalse(result["ok"])
        self.assertIn("WIGGUM_LEGACY_DIAGNOSTIC_ONLY", result["reason"])

    def test_installed_legacy_auditor_and_wiggum_paths_cannot_count_as_authority(self) -> None:
        for authority_ref in (
            str(OPENCLAW_HOME / "scripts" / "run-auditor.sh"),
            str(OPENCLAW_HOME / "wiggum" / "auto-auditors.sh"),
        ):
            with self.subTest(authority_ref=authority_ref):
                scope = claim_scope(stateAuthorityRefs=[{"path": authority_ref}])

                result = self.guard(finalizer_with_scope(scope))

                self.assertFalse(result["ok"])
                self.assertIn("WIGGUM_LEGACY_DIAGNOSTIC_ONLY", result["reason"])

    def test_raw_failed_diagnostic_cannot_be_normalized_into_acceptance(self) -> None:
        scope = claim_scope(rawDiagnostics=["success marker plus required-read failure in raw trace"])

        result = self.guard(finalizer_with_scope(scope))

        self.assertFalse(result["ok"])
        self.assertIn("RAW_EVIDENCE_BLOCKED", result["reason"])

    def test_raw_evidence_ref_file_failure_cannot_be_laundered_by_metadata(self) -> None:
        raw_trace = RAW_EVIDENCE_ROOT / "tmp-finalizer-raw-trace-regression.txt"
        raw_trace.write_text("success marker\nfailed required read: BOOT.md\n", encoding="utf-8")
        try:
            scope = claim_scope(
                rawEvidenceRefs=[
                    {
                        "path": raw_trace.name,
                        "kind": "raw_trace",
                        "validation": {
                            "status": "accepted",
                            "noFailedRequiredReads": True,
                            "rawDiagnosticsPreserved": True,
                            "failedRequiredReads": False,
                            "stderrContradictions": False,
                            "nonzeroExitsNormalizedAway": False,
                        },
                    }
                ],
                rawDiagnostics=[],
            )

            result = self.guard(finalizer_with_scope(scope))
        finally:
            raw_trace.unlink(missing_ok=True)

        self.assertFalse(result["ok"])
        self.assertIn("RAW_EVIDENCE_BLOCKED", result["reason"])
        self.assertIn("claimScope.rawEvidenceRefs[0] contains failed required diagnostics", result["reason"])

    def test_render_missing_claim_scope_is_failed_not_legacy_passed(self) -> None:
        issue = issue_with_finalizer(finalizer_with_scope(None))

        rendered = finalizer_required.render_status(issue)
        summary = finalizer_required.finalizer_summary(issue)

        self.assertFalse(rendered["ok"])
        self.assertEqual(rendered["status"], "failed")
        self.assertNotEqual(rendered.get("verdict"), "passed_legacy_unscoped")
        self.assertNotIn("passed_legacy_unscoped", summary or "")
        self.assertNotIn("finalizer=passed", summary or "")

    def test_subject_must_match_issue_id(self) -> None:
        result = self.guard(finalizer_with_scope(claim_scope(subject="other-issue")))

        self.assertFalse(result["ok"])
        self.assertIn("subject must match issue id", result["reason"])

    def test_claim_scope_commit_and_destination_must_match_finalizer(self) -> None:
        result = self.guard(finalizer_with_scope(claim_scope(subjectCommitOid="def456", destinationRef="refs/heads/other")))

        self.assertFalse(result["ok"])
        self.assertIn("subjectCommit/subjectCommitOid must match", result["reason"])
        self.assertIn("destinationRef must match", result["reason"])

    def test_reviewer_refs_and_finalizer_identity_require_matching_claim_metadata(self) -> None:
        scope = claim_scope()
        scope["reviewerEvidenceRefs"] = [{"path": "reviews/primary.json"}]
        scope["finalizerIdentity"] = {
            "role": "mediator_finalizer",
            "sessionOrRunId": "run-123",
            "artifactRef": "reviews/mediator.json",
        }

        result = self.guard(finalizer_with_scope(scope))

        self.assertFalse(result["ok"])
        self.assertIn("reviewerEvidenceRefs[0].subject must match", result["reason"])
        self.assertIn("finalizerIdentity.subject must match", result["reason"])

    def test_project_acceptance_claim_requires_project_acceptance_tier_and_evidence(self) -> None:
        result = self.guard(
            finalizer_with_scope(
                claim_scope(
                    requestedClaim="project acceptance green for OpenClaw",
                    allowedClaims=["project green"],
                    forbiddenClaims=["Overlay module acceptance"],
                )
            )
        )

        self.assertFalse(result["ok"])
        self.assertIn("cannot claim project acceptance", result["reason"])

        project_scope = claim_scope(
            requestedClaim="project acceptance green for OpenClaw",
            proofTier="project_acceptance",
            allowedClaims=["project green"],
            forbiddenClaims=["Overlay module acceptance"],
        )
        result = self.guard(finalizer_with_scope(project_scope))
        self.assertFalse(result["ok"])
        self.assertIn("projectEvidenceRefs", result["reason"])

    def test_allowed_claims_cannot_contradict_forbidden_claims(self) -> None:
        result = self.guard(
            finalizer_with_scope(
                claim_scope(
                    allowedClaims=["candidate guard enforces process invariant"],
                    forbiddenClaims=["candidate guard enforces process invariant"],
                )
            )
        )

        self.assertFalse(result["ok"])
        self.assertIn("contradict forbiddenClaims", result["reason"])

    def test_allowed_claims_must_include_requested_claim(self) -> None:
        result = self.guard(
            finalizer_with_scope(
                claim_scope(
                    allowedClaims=["adjacent scoped claim"],
                )
            )
        )

        self.assertFalse(result["ok"])
        self.assertIn("allowedClaims must include the exact requestedClaim", result["reason"])

    def test_module_acceptance_claim_requires_module_acceptance_tier(self) -> None:
        result = self.guard(
            finalizer_with_scope(
                claim_scope(
                    requestedClaim="Overlay module acceptance for Module X",
                    allowedClaims=["Overlay module acceptance for Module X"],
                    proofTier="runtime_parity",
                )
            )
        )

        self.assertFalse(result["ok"])
        self.assertIn("proofTier>=module_acceptance", result["reason"])

    def test_raw_evidence_refs_require_source_run_or_exit_provenance(self) -> None:
        result = self.guard(
            finalizer_with_scope(
                claim_scope(
                    rawEvidenceRefs=[{"path": "evidence/raw-command.json", "kind": "validator_json"}],
                )
            )
        )

        self.assertFalse(result["ok"])
        self.assertIn("source_hash, run identity, or exit_code provenance", result["reason"])

    def test_local_raw_evidence_ref_missing_sha256_blocks(self) -> None:
        refs = structured_raw_evidence_refs()
        refs[0].pop("sha256")

        result = self.guard(finalizer_with_scope(claim_scope(rawEvidenceRefs=refs)))

        self.assertFalse(result["ok"])
        self.assertIn("claimScope.rawEvidenceRefs[0].sha256 is required", result["reason"])

    def test_local_raw_evidence_ref_missing_validation_blocks(self) -> None:
        refs = structured_raw_evidence_refs()
        refs[0].pop("validation")

        result = self.guard(finalizer_with_scope(claim_scope(rawEvidenceRefs=refs)))

        self.assertFalse(result["ok"])
        self.assertIn("claimScope.rawEvidenceRefs[0].validation must be a structured object", result["reason"])

    def test_fully_structured_raw_evidence_refs_are_accepted(self) -> None:
        result = self.guard(finalizer_with_scope(claim_scope()))

        self.assertTrue(result["ok"], result)

    def test_omitted_raw_diagnostics_require_structured_sticky_limitations(self) -> None:
        result = self.guard(
            finalizer_with_scope(
                claim_scope(
                    omittedRawDiagnostics=["stderr omitted by normalizer"],
                )
            )
        )

        self.assertFalse(result["ok"])
        self.assertIn("RAW_EVIDENCE_BLOCKED", result["reason"])
        self.assertIn("marker-only text", result["reason"])

    def test_reviewer_refs_must_match_review_route(self) -> None:
        scope = claim_scope()
        scope["reviewerEvidenceRefs"][0]["reviewRoute"] = {
            "reviewProfile": "security_code",
            "reviewTier": "primary_checker",
        }

        result = self.guard(finalizer_with_scope(scope))

        self.assertFalse(result["ok"])
        self.assertIn("reviewProfile does not match", result["reason"])
        self.assertIn("reviewTier does not match", result["reason"])

    def test_finalizer_identity_role_is_constrained(self) -> None:
        scope = claim_scope()
        scope["finalizerIdentity"]["role"] = "random_judge"

        result = self.guard(finalizer_with_scope(scope))

        self.assertFalse(result["ok"])
        self.assertIn("role must be mediator_finalizer, finalizer, or human", result["reason"])

    def test_local_only_arbitrary_done_status_blocks(self) -> None:
        issue = {
            "id": "local-banana",
            "project": "(framework)",
            "state": "Human Review",
            "allowedPaths": ["docs/local-note.md"],
            "status": {
                "finalizer": {
                    "localOnly": {
                        "paths": ["docs/local-note.md"],
                        "rationale": "documentation-only local candidate",
                        "completionMode": "local-only-human-review",
                        "doneStatus": "BANANA",
                    }
                }
            },
        }

        result = finalizer_required.finalizer_transition_guard(issue, "Human Review")

        self.assertFalse(result["ok"])
        self.assertIn("doneStatus is not one of the constrained allowed values", result["reason"])


if __name__ == "__main__":
    unittest.main()
