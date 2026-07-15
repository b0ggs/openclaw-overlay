#!/usr/bin/env python3
"""Pure execution-pattern router for orchestrator dispatch decisions.

The router is intentionally side-effect free: it reads only the objects passed to
``derive_execution_pattern`` and returns an issue patch/recommendation for the
orchestrator to persist.  Callers choose whether to keep the decision shadow-only
or to enforce the safe, direct-dispatch patch before launching a worker.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.path_config import workspace_root

ROUTER_VERSION = "execution-pattern-router-v1"

CANONICAL_EXECUTION_PATTERNS = {
    "code",
    "scored_code",
    "research_report",
    "critic",
    "optimize_single",
    "optimize_fanout",
    "flatline_entropy",
    "evaluator_exploit_review",
    "truth_repair",
    "finalization",
    "pipeline",
    "harness_candidate_trial",
}

EXECUTION_PATTERN_ALIASES = {
    "bridge_critic": "critic",
}

WORKER_MODE_BY_PATTERN = {
    "code": "code",
    "scored_code": "code",
    "research_report": "research",
    "critic": "bridge_research",
    "optimize_single": "optimize",
    "optimize_fanout": "optimize",
    "flatline_entropy": "bridge_research",
    "evaluator_exploit_review": "bridge_research",
    "truth_repair": "code",
    "finalization": "optimize",
    "pipeline": "pipeline",
    "harness_candidate_trial": "bridge_research",
}

REEVALUATE_ON = [
    "issue_state_change",
    "worker_mode_change",
    "research_state_change",
    "truth_status_change",
    "review_feedback_change",
    "scoring_block_change",
    "frozen_authorization_change",
]

CONTROL_PLANE_PREFIXES = (
    "scripts/",
    "state/",
    "harness/",
    "templates/",
    "cron/",
    "docs/design/",
)

CONTROL_PLANE_ROOT_FILES = {
    "AGENTS.md",
    "ANALYST.md",
    "BOOT.md",
    "CODER.md",
    "CODEX.md",
    "IDENTITY.md",
    "ORCHESTRATOR.md",
    "PIPELINER.md",
    "RESEARCHER.md",
    "REVIEWER_DATA.md",
    "REVIEWER_OPS.md",
    "SKILLS_ROUTING.md",
    "SOUL.md",
    "STATE.md",
    "SUPERVISOR.md",
    "TOOLS.md",
    "USER.md",
    "WORKFLOW.md",
}

TERMINAL_STATES = {"Done", "Cancelled", "Canceled", "Duplicate", "Blocked"}
COMPLETE_STATES = {"Done", "Human Review"}

FLATLINE_STEPS = [
    {"id": "literature_refresh", "kind": "bridge_research"},
    {"id": "alt_framing_critic", "kind": "bridge_research"},
    {"id": "reset_blind_lane", "kind": "optimize"},
    {"id": "champion_local_exploit", "kind": "optimize"},
]

FLATLINE_STEP_SUFFIX = {
    "literature_refresh": "flatline-literature",
    "alt_framing_critic": "critic",
    "reset_blind_lane": "reset",
    "champion_local_exploit": "exploit",
}


def canonicalize_execution_pattern(raw: Any) -> str | None:
    """Return the canonical execution pattern name, if recognized."""

    if raw in (None, ""):
        return None
    value = str(raw).strip()
    if not value:
        return None
    value = EXECUTION_PATTERN_ALIASES.get(value, value)
    return value if value in CANONICAL_EXECUTION_PATTERNS else None


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_obj(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(_stable_json(obj).encode("utf-8")).hexdigest()


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    return [value]


def _non_empty_str(value: Any) -> str:
    return str(value or "").strip()


def _truthy_explicit(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "required", "pending"}


def _canonical_path(path: Any) -> str:
    raw = str(path or "").strip().replace("\\", "/")
    raw = raw.replace("$HOME/", "~/")
    configured_root = str(workspace_root()).rstrip("/").replace("\\", "/")
    raw = re.sub(rf"^{re.escape(configured_root)}/", "", raw)
    raw = re.sub(r"^~?/\.openclaw/workspace/", "", raw)
    raw = re.sub(r"^\./", "", raw)
    while "//" in raw:
        raw = raw.replace("//", "/")
    return raw.strip("/")


def _collect_paths(issue: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in (
        "allowedPaths",
        "touchedPaths",
        "outputPaths",
        "inputPaths",
        "changedPaths",
        "editablePaths",
        "lockedPaths",
    ):
        paths.extend(_canonical_path(p) for p in _as_list(issue.get(key)))
    for container_key in ("maintenance", "harnessCandidate", "retrospective"):
        container = _as_dict(issue.get(container_key))
        for key in ("touchedPaths", "outputPaths", "allowedPaths"):
            paths.extend(_canonical_path(p) for p in _as_list(container.get(key)))
    return [p for p in paths if p]


def _path_touches_framework_or_runtime(path: str) -> bool:
    if not path:
        return False
    if path in CONTROL_PLANE_ROOT_FILES:
        return True
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in CONTROL_PLANE_PREFIXES)


def _touches_framework_or_runtime(issue: dict[str, Any]) -> bool:
    if str(issue.get("project") or "") == "(framework)":
        return True
    if _truthy_explicit(issue.get("touchesFrameworkOrRuntime")):
        return True
    for item in _as_list(issue.get("pathClassifications")):
        if str(item or "").strip() in {"framework", "runtime", "control_plane", "harness"}:
            return True
    return any(_path_touches_framework_or_runtime(path) for path in _collect_paths(issue))


def _program(research_ctx: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(research_ctx.get("program"))


def _state(research_ctx: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(research_ctx.get("state"))


def _evaluation_cfg(program: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(program.get("evaluation"))


def _research_cfg(program: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(program.get("research"))


def _promotion_cfg(program: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(program.get("promotion"))


def _canonical_artifacts(program: dict[str, Any]) -> list[str]:
    research = _research_cfg(program)
    artifacts = [_canonical_path(p) for p in _as_list(research.get("canonical_artifacts"))]
    return [p for p in artifacts if p]


def _artifact_root(program: dict[str, Any]) -> str:
    return _canonical_path(_research_cfg(program).get("artifact_root"))


def _path_within(path: str, root_or_file: str) -> bool:
    if not path or not root_or_file:
        return False
    root_or_file = root_or_file.rstrip("/")
    return path == root_or_file or path.startswith(root_or_file + "/")


def _editable_surface_enforced(issue: dict[str, Any], program: dict[str, Any]) -> bool:
    allowed = [_canonical_path(p) for p in _as_list(issue.get("allowedPaths")) if _canonical_path(p)]
    if not allowed:
        return False
    artifacts = _canonical_artifacts(program)
    artifact_root = _artifact_root(program)
    if not artifacts and not artifact_root:
        return False
    for path in allowed:
        if artifacts and any(_path_within(path, artifact) for artifact in artifacts):
            continue
        if artifact_root and _path_within(path, artifact_root):
            continue
        return False
    return True


def _forbidden_surface_tokens(program: dict[str, Any]) -> set[str]:
    research = _research_cfg(program)
    evaluation = _evaluation_cfg(program)
    tokens = {
        "evaluator",
        "validator",
        "benchmark",
        "benchmarks",
        "dataset",
        "datasets",
        "data",
        "setup",
        "runtime",
        "platform",
        "research/results.tsv",
        "research/champion.yaml",
        "research/state.json",
    }
    for key in ("immutable_paths", "forbidden_paths", "evaluator_paths", "data_paths", "setup_paths"):
        for path in _as_list(research.get(key)) + _as_list(evaluation.get(key)):
            canonical = _canonical_path(path).lower()
            if canonical:
                tokens.add(canonical)
    return tokens


def _immutable_surfaces(issue: dict[str, Any], program: dict[str, Any]) -> tuple[bool, bool]:
    allowed = [_canonical_path(p).lower() for p in _as_list(issue.get("allowedPaths")) if _canonical_path(p)]
    if not allowed:
        return False, False
    forbidden = _forbidden_surface_tokens(program)
    for path in allowed:
        parts = path.split("/")
        if any(
            path == token
            or path.startswith(token.rstrip("/") + "/")
            or token in parts
            or any(part == token or part.startswith(token + ".") for part in parts)
            for token in forbidden
        ):
            return False, False
    return True, True


def _metric_comparable(program: dict[str, Any]) -> bool:
    evaluation = _evaluation_cfg(program)
    metric = _non_empty_str(evaluation.get("metric"))
    direction = _non_empty_str(evaluation.get("direction")).lower()
    seeds = _as_dict(evaluation.get("seeds"))
    has_seed_policy = any(seeds.get(key) not in (None, "") for key in ("quick", "confirm", "promotion", "final"))
    return bool(metric and direction in {"maximize", "minimize", "max", "min"} and has_seed_policy)


def _fixed_candidate_budget(program: dict[str, Any]) -> bool:
    evaluation = _evaluation_cfg(program)
    budget = _as_dict(evaluation.get("budget"))
    tiered = _as_dict(evaluation.get("tiered_eval"))
    seeds = _as_dict(evaluation.get("seeds"))
    has_budget = any(budget.get(key) not in (None, "", 0) for key in ("total_evaluations", "total_tokens", "per_candidate_seconds", "per_candidate_evaluations"))
    has_seed_policy = any(seeds.get(key) not in (None, "") for key in ("quick", "confirm", "promotion", "final"))
    has_tier = bool(tiered) or has_seed_policy
    return bool(has_budget and has_tier)


def _fixed_evaluator(program: dict[str, Any]) -> bool:
    evaluation = _evaluation_cfg(program)
    return bool(_non_empty_str(evaluation.get("evaluator_command")))


def _validator_present(program: dict[str, Any]) -> bool:
    evaluation = _evaluation_cfg(program)
    promotion = _promotion_cfg(program)
    required = promotion.get("validator_required", True) is not False
    return bool(_non_empty_str(evaluation.get("validator_command"))) or not required


def _single_or_enumerated_artifact_surface(program: dict[str, Any]) -> bool:
    artifacts = _canonical_artifacts(program)
    return bool(artifacts) and len(artifacts) <= 8


def _truth_status(research_ctx: dict[str, Any]) -> str | None:
    state = _state(research_ctx)
    raw = state.get("truthStatus")
    if raw in (None, ""):
        raw = research_ctx.get("truthStatus")
    if raw in (None, ""):
        return None
    return str(raw).strip().lower()


def _living_distillation_pending(research_ctx: dict[str, Any]) -> bool:
    distillation = _as_dict(_state(research_ctx).get("distillation"))
    if not distillation:
        return False
    if _truthy_explicit(distillation.get("pending")) or _truthy_explicit(distillation.get("updateRequired")):
        return True
    status = str(distillation.get("status") or "").strip().lower()
    if status in {"pending", "required", "stale"}:
        return True
    pending_events = distillation.get("pendingEvents")
    return isinstance(pending_events, list) and len(pending_events) > 0


def _evaluator_exploit_review_pending(research_ctx: dict[str, Any]) -> bool:
    review = _as_dict(_state(research_ctx).get("evaluatorExploitReview"))
    return str(review.get("status") or "").strip().lower() == "pending"


def _flatline_recovery_state(research_ctx: dict[str, Any]) -> dict[str, Any]:
    state = _state(research_ctx)
    program = _program(research_ctx)
    flatline = _as_dict(state.get("flatline"))
    promotion = _promotion_cfg(program)
    try:
        incremental_threshold = int(promotion.get("flatline_threshold_incremental") or 3)
    except Exception:
        incremental_threshold = 3
    try:
        total_threshold = int(promotion.get("flatline_threshold_total") or 12)
    except Exception:
        total_threshold = 12
    try:
        incremental_non = int(flatline.get("incrementalNonImprovements") or 0)
    except Exception:
        incremental_non = 0
    try:
        total_non = int(flatline.get("totalNonImprovements") or 0)
    except Exception:
        total_non = 0
    try:
        resets_attempted = int(flatline.get("resetsAttempted") or 0)
    except Exception:
        resets_attempted = 0
    proxy_mismatch = bool(
        _as_dict(state.get("truth")).get("proxyTruthMismatch")
        or state.get("proxyTruthMismatch")
        or _as_dict(state.get("truthManager")).get("proxyTruthMismatch")
        or _as_dict(research_ctx.get("truthManager")).get("proxyTruthMismatch")
    )
    phase = str(state.get("phase") or "")
    return {
        "needReset": phase == "search" and incremental_threshold > 0 and incremental_non >= incremental_threshold and resets_attempted == 0 and total_non < total_threshold,
        "needCritic": phase in {"search", "blocked_flatline"}
        and ((incremental_threshold > 0 and incremental_non >= incremental_threshold and resets_attempted > 0) or proxy_mismatch or phase == "blocked_flatline"),
        "proxyMismatch": proxy_mismatch,
        "incrementalNonImprovements": incremental_non,
        "totalNonImprovements": total_non,
        "resetsAttempted": resets_attempted,
    }


def _flatline_active(research_ctx: dict[str, Any]) -> bool:
    state = _state(research_ctx)
    flatline = _as_dict(state.get("flatline"))
    playbook = _as_dict(flatline.get("playbook"))
    if playbook.get("active"):
        return True
    if str(state.get("phase") or "") == "blocked_flatline":
        return True
    recovery = _flatline_recovery_state(research_ctx)
    proxy_only = bool(
        recovery.get("proxyMismatch")
        and not recovery.get("needReset")
        and int(recovery.get("incrementalNonImprovements") or 0) == 0
        and int(recovery.get("resetsAttempted") or 0) == 0
        and str(state.get("phase") or "") == "search"
    )
    return bool(recovery.get("needReset") or (recovery.get("needCritic") and not proxy_only))


def _flatline_step_issue_id(parent_id: str, step_id: str, run_seq: int = 1) -> str:
    suffix = FLATLINE_STEP_SUFFIX.get(step_id, step_id.replace("_", "-"))
    if int(run_seq or 1) <= 1:
        return f"{parent_id}-{suffix}"
    return f"{parent_id}-{suffix}-r{int(run_seq)}"


def _first_incomplete_flatline_step(parent_id: str, research_ctx: dict[str, Any], issues_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    state = _state(research_ctx)
    flatline = _as_dict(state.get("flatline"))
    playbook = _as_dict(flatline.get("playbook"))
    raw_steps = playbook.get("steps") if isinstance(playbook.get("steps"), list) else FLATLINE_STEPS
    steps = [step for step in raw_steps if isinstance(step, dict) and _non_empty_str(step.get("id"))]
    if not steps:
        steps = FLATLINE_STEPS
    try:
        run_seq = int(playbook.get("runSeq") or 1)
    except Exception:
        run_seq = 1
    step_status = _as_dict(playbook.get("stepStatus"))
    for index, step in enumerate(steps, start=1):
        step_id = _non_empty_str(step.get("id"))
        helper_id = _flatline_step_issue_id(parent_id, step_id, run_seq=run_seq)
        issue_state = str(_as_dict(issues_by_id.get(helper_id)).get("state") or "")
        status = str(step_status.get(step_id) or "").strip().lower()
        if issue_state in COMPLETE_STATES or status == "complete":
            continue
        return {
            "stepId": step_id,
            "index": index,
            "total": len(steps),
            "issueId": helper_id,
            "kind": step.get("kind") or ("optimize" if step_id in {"reset_blind_lane", "champion_local_exploit"} else "bridge_research"),
        }
    return {"stepId": None, "issueId": None, "kind": None, "index": None, "total": len(steps)}


def _evaluator_review_issue_id(parent_id: str, research_ctx: dict[str, Any]) -> str:
    review = _as_dict(_state(research_ctx).get("evaluatorExploitReview"))
    raw_reason = str(review.get("pendingReason") or "review").strip().lower().replace("_", "-")
    safe_reason = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in raw_reason).strip("-") or "review"
    try:
        sequence = int(review.get("sequence") or 1)
    except Exception:
        sequence = 1
    return f"{parent_id}-evaluator-exploit-{safe_reason}-{max(1, sequence)}"


def _distillation_issue_id(parent_id: str, research_ctx: dict[str, Any]) -> str:
    distillation = _as_dict(_state(research_ctx).get("distillation"))
    try:
        sequence = int(distillation.get("sequence") or 1)
    except Exception:
        sequence = 1
    return f"{parent_id}-distillation-{max(1, sequence)}"


def _truth_repair_needed(research_ctx: dict[str, Any]) -> tuple[bool, str, str]:
    state = _state(research_ctx)
    truth = _as_dict(state.get("truth"))
    source = str(truth.get("lastFailureSource") or "")
    detail = str(truth.get("lastFailureDetail") or "")
    combined = f"{source} {detail}".lower()
    if any(token in combined for token in ["budget", "quota exhausted", "token", "evaluation budget exhausted"]):
        return False, source, detail
    if any(token in combined for token in ["evaluator", "validator", "traceback", "json", "stdout", "stderr", "timeout", "workspace", "command failed"]):
        return True, source, detail
    return False, source, detail


def _parent_optimize_issue(issue: dict[str, Any], issues_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    parent_id = _non_empty_str(issue.get("parentOptimizeIssue"))
    if parent_id and isinstance(issues_by_id.get(parent_id), dict):
        return issues_by_id[parent_id]
    return issue


def _helper_action(kind: str = "none", issue_id: str | None = None, *, frozen_window: bool = False, approved_ids: set[str] | None = None, safety_hold: bool = False) -> dict[str, Any]:
    approved_ids = approved_ids or set()
    dispatch_allowed = bool(issue_id and (not frozen_window or issue_id in approved_ids))
    if not issue_id:
        disposition = "not_applicable"
    elif not frozen_window:
        disposition = "not_applicable"
    elif issue_id in approved_ids:
        disposition = "approved_helper"
    else:
        disposition = "blocked_unapproved_helper" if safety_hold else "queued_after_window"
    return {
        "kind": kind,
        "issueId": issue_id,
        "dispatchAllowed": dispatch_allowed,
        "frozenWindowDisposition": disposition,
    }


def _optimize_prerequisites(signals: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not signals.get("explicitScoreDrivenArtifactSearch"):
        missing.append("issue does not explicitly declare score-driven artifact search")
    checks = [
        ("hasResearchProgram", "missing RESEARCH_PROGRAM.md/runtime research context"),
        ("programConfigPresent", "missing research program configuration"),
        ("fixedEvaluator", "missing fixed evaluator command"),
        ("validatorPresent", "missing validator command"),
        ("fixedCandidateBudget", "missing fixed candidate budget/seed tier"),
        ("metricComparable", "missing comparable metric/direction/seed policy"),
        ("editableSurfaceEnforced", "editable artifact surface is not enforced"),
        ("immutableEvaluatorSurface", "evaluator/data/setup surface is mutable"),
        ("immutableRuntimePlatform", "runtime platform is not immutable/comparable"),
        ("singleOrEnumeratedArtifactSurface", "artifact surface is not single/enumerated"),
    ]
    for key, reason in checks:
        if not signals.get(key):
            missing.append(reason)
    if signals.get("truthStatus") not in {"available", None}:
        missing.append("truth is unavailable")
    if signals.get("researchPhase") not in {"search", None}:
        missing.append(f"research phase is not search ({signals.get('researchPhase') or 'unknown'})")
    if not signals.get("candidateGenerationAllowed"):
        missing.append("candidate generation is not currently allowed")
    return missing


def _candidate_generation_action(issue: dict[str, Any]) -> bool:
    return str(issue.get("researchAction") or "candidate_generation") == "candidate_generation"


def _explicit_score_driven_artifact_search(issue: dict[str, Any]) -> bool:
    """Return true only for deterministic optimize/candidate-search intent.

    A scoring block alone is intentionally not enough: measurable engineering
    work remains ``scored_code`` unless the issue explicitly declares candidate
    artifact search/optimization intent and all optimize prerequisites pass.
    """

    worker_mode = str(issue.get("workerMode") or "").strip()
    if worker_mode == "optimize":
        return True
    if issue.get("researchCritic") or issue.get("researchDistillation") or issue.get("criticType"):
        return False
    objective = _as_dict(issue.get("objective"))
    optimization = _as_dict(issue.get("optimization"))
    objective_kind = str(objective.get("kind") or objective.get("type") or "").strip().lower()
    optimization_kind = str(optimization.get("kind") or optimization.get("type") or "").strip().lower()
    explicit_kinds = {
        "artifact_search",
        "candidate_search",
        "score_search",
        "optimization",
        "optimize",
        "autoresearch",
        "auto_research",
    }
    if objective_kind in explicit_kinds or optimization_kind in explicit_kinds:
        return True
    if _truthy_explicit(objective.get("optimization")) or _truthy_explicit(objective.get("candidateSearch")):
        return True
    if _truthy_explicit(optimization.get("enabled")) or _truthy_explicit(optimization.get("candidateSearch")):
        return True
    for key in ("scoreDrivenArtifactSearch", "optimizeCandidateSearch", "candidateGeneration"):
        if _truthy_explicit(issue.get(key)):
            return True
    return False


def _review_or_human_unresolved(issue: dict[str, Any]) -> bool:
    if _truthy_explicit(issue.get("humanDecisionRequired")):
        return True
    if _truthy_explicit(issue.get("requiresHumanJudgment")):
        return True
    if _truthy_explicit(issue.get("planningRequired")):
        return True
    if issue.get("successCriteriaClear") is False:
        return True
    gate = _as_dict(issue.get("gate")) or _as_dict(issue.get("projectInitiation"))
    if str(gate.get("status") or "").strip().lower() in {"unresolved", "requires_human", "pending_human"}:
        return True
    if str(issue.get("state") or "") == "Human Review":
        return True
    design_kind = str(issue.get("designKind") or issue.get("planningKind") or "").strip().lower()
    if design_kind in {"architecture", "policy", "evaluation_design", "governance"} and not (issue.get("writtenPlan") or issue.get("planFile") or issue.get("proofObjective")):
        return True
    return False


def _research_or_critic_pattern(issue: dict[str, Any]) -> str:
    existing = canonicalize_execution_pattern(issue.get("executionPattern"))
    if existing in {"critic", "evaluator_exploit_review", "flatline_entropy"}:
        return "critic" if existing == "critic" else existing
    if issue.get("researchCritic") or issue.get("researchDistillation") or issue.get("evaluatorExploitReview") or issue.get("criticType"):
        return "critic"
    if str(issue.get("workerMode") or "") == "bridge_research":
        return "critic"
    return "research_report"


def _apply_pattern_hints(issue: dict[str, Any], selected_pattern: str, status: str, alternatives: list[str], signals: dict[str, Any]) -> str:
    hints = _as_dict(issue.get("patternHints"))
    if not hints:
        signals["patternHintsConflict"] = False
        return status
    allowed = {canonicalize_execution_pattern(p) for p in _as_list(hints.get("allowedPatterns"))}
    allowed.discard(None)
    forbidden = {canonicalize_execution_pattern(p) for p in _as_list(hints.get("forbiddenPatterns"))}
    forbidden.discard(None)
    conflict = False
    if allowed and selected_pattern not in allowed:
        conflict = True
        alternatives.append(
            "patternHints.allowedPatterns conflict: safe runtime pattern "
            f"{selected_pattern} is not in {sorted(allowed)}"
        )
    if selected_pattern in forbidden:
        conflict = True
        alternatives.append(f"patternHints.forbiddenPatterns conflict: safe runtime pattern {selected_pattern} is forbidden")
    signals["patternHintsConflict"] = conflict
    if conflict:
        return "blocked_pattern_hint_conflict"
    return status


def _extract_signals(issue: dict[str, Any], *, orch: dict[str, Any], wf: dict[str, Any], research_ctx: dict[str, Any], issues_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    program = _program(research_ctx)
    state = _state(research_ctx)
    truth_manager = _as_dict(research_ctx.get("truthManager")) or _as_dict(state.get("truthManager"))
    evaluation = _evaluation_cfg(program)
    research = _research_cfg(program)
    immutable_evaluator, immutable_runtime = _immutable_surfaces(issue, program)
    budget = _as_dict(research_ctx.get("budget"))
    remaining_pct = budget.get("remainingEvaluationsPct")
    if remaining_pct in (None, ""):
        remaining_pct = truth_manager.get("budgetRemainingPct")
    try:
        remaining_pct = float(remaining_pct) if remaining_pct not in (None, "") else None
    except Exception:
        remaining_pct = None

    research_cfg = _as_dict(wf.get("research"))
    fanout_heuristics = _as_dict(research_cfg.get("fanout_heuristics"))
    try:
        max_latency = float(fanout_heuristics.get("max_eval_latency_seconds_for_fanout") or 120)
    except Exception:
        max_latency = 120.0
    latency = truth_manager.get("evaluatorLatencySeconds")
    if latency in (None, ""):
        latency = _as_dict(state.get("truth")).get("evaluatorLatencySeconds")
    try:
        latency_float = float(latency) if latency not in (None, "") else None
    except Exception:
        latency_float = None
    throughput_bottleneck = bool(
        truth_manager.get("evaluatorThroughputBottleneck")
        or truth_manager.get("throughputBottleneck")
        or (latency_float is not None and latency_float > max_latency)
    )

    flatline = _as_dict(state.get("flatline"))
    playbook = _as_dict(flatline.get("playbook"))
    frozen_window = bool(orch.get("authorizationFrozenAt"))
    approved_ids = sorted({str(i) for i in _as_list(orch.get("approvedIssueIds")) if str(i)})
    proxy_truth_mismatch = bool(
        truth_manager.get("proxyTruthMismatch")
        or _as_dict(state.get("truth")).get("proxyTruthMismatch")
        or state.get("proxyTruthMismatch")
    )
    parallel_cfg = _as_dict(research_cfg.get("parallel"))
    explicit_lane_ready = research_ctx.get("laneIsolationReady")
    lane_ready = bool(explicit_lane_ready) if explicit_lane_ready is not None else bool(issue.get("optimizeLane") and issue.get("workspace"))
    phase = str(state.get("phase") or "") or None
    truth_status = _truth_status(research_ctx)
    candidate_allowed = truth_manager.get("candidateGenerationAllowed")
    if candidate_allowed is None:
        candidate_allowed = bool(phase == "search" and truth_status in {"available", None})

    status = _as_dict(issue.get("status"))
    retry_count = int(status.get("retryCount") or 0)
    handoff_repair_count = int(status.get("handoffRepairCount") or 0)

    return {
        "existingExecutionPattern": canonicalize_execution_pattern(issue.get("executionPattern")),
        "existingExecutionPatternRaw": issue.get("executionPattern"),
        "explicitScoreDrivenArtifactSearch": _explicit_score_driven_artifact_search(issue),
        "touchesFrameworkOrRuntime": _touches_framework_or_runtime(issue),
        "hasScoringBlock": isinstance(issue.get("scoring"), dict) and bool(issue.get("scoring")),
        "hasResearchProgram": bool(research_ctx.get("enabled") and program),
        "hasComparableResearchContract": False,  # filled below
        "programConfigPresent": bool(program),
        "fixedEvaluator": _fixed_evaluator(program),
        "validatorPresent": _validator_present(program),
        "fixedCandidateBudget": _fixed_candidate_budget(program),
        "metricComparable": _metric_comparable(program),
        "editableSurfaceEnforced": _editable_surface_enforced(issue, program),
        "immutableEvaluatorSurface": immutable_evaluator,
        "immutableRuntimePlatform": immutable_runtime,
        "singleOrEnumeratedArtifactSurface": _single_or_enumerated_artifact_surface(program),
        "hasEvaluatorCommand": bool(_non_empty_str(evaluation.get("evaluator_command"))),
        "hasValidatorCommand": bool(_non_empty_str(evaluation.get("validator_command"))),
        "researchPhase": phase,
        "truthStatus": truth_status,
        "candidateGenerationAllowed": bool(candidate_allowed),
        "budgetRemainingPct": remaining_pct,
        "deadlinePassed": bool(research_ctx.get("deadlinePassed") or _as_dict(state.get("deadline")).get("deadlinePassedAt")),
        "withinSubmissionFreeze": bool(
            research_ctx.get("withinFreezeWindow")
            or research_ctx.get("consolidationOnly")
            or phase == "submission_freeze"
            or research_ctx.get("deadlinePassed")
        ),
        "flatlineActive": _flatline_active(research_ctx),
        "flatlinePlaybookActive": bool(playbook.get("active")),
        "evaluatorExploitReviewPending": _evaluator_exploit_review_pending(research_ctx),
        "livingDistillationPending": _living_distillation_pending(research_ctx),
        "proxyTruthMismatch": proxy_truth_mismatch,
        "laneIsolationReady": lane_ready,
        "workerBlockedRepeatedly": bool(status.get("repeatedDeterministicFailure") or retry_count >= 3 or handoff_repair_count >= 2),
        "reviewDisagreementActive": bool(issue.get("reviewDisagreement") or status.get("reviewDisagreement") or (issue.get("state") == "Rework" and status.get("lastReviewAt"))),
        "evaluatorThroughputBottleneck": throughput_bottleneck,
        "evaluatorLatencySeconds": latency_float,
        "maintenanceKind": _as_dict(issue.get("maintenance")).get("kind"),
        "frozenWindow": frozen_window,
        "approvedHelperIds": approved_ids,
        "reviewTier": issue.get("reviewTier") or _as_dict(issue.get("meta")).get("reviewTier"),
        "patternHintsConflict": False,
        "autoFanoutEnabled": bool(parallel_cfg.get("auto_fanout") is True),
        "projectFanoutEnabled": bool(_as_dict(research.get("fanout")).get("auto", True) is not False),
        "unclearOrHumanJudgment": _review_or_human_unresolved(issue),
    }


def _finalize_comparable_contract(signals: dict[str, Any]) -> None:
    signals["hasComparableResearchContract"] = all(
        bool(signals.get(key))
        for key in (
            "hasResearchProgram",
            "programConfigPresent",
            "fixedEvaluator",
            "validatorPresent",
            "fixedCandidateBudget",
            "metricComparable",
            "editableSurfaceEnforced",
            "immutableEvaluatorSurface",
            "immutableRuntimePlatform",
            "singleOrEnumeratedArtifactSurface",
        )
    )


def _stable_input_subset(
    issue: dict[str, Any],
    *,
    orch: dict[str, Any],
    wf: dict[str, Any],
    research_ctx: dict[str, Any],
    issues_by_id: dict[str, dict[str, Any]],
    signals: dict[str, Any],
) -> dict[str, Any]:
    issue_keys = [
        "id",
        "kind",
        "project",
        "state",
        "workerMode",
        "executionPattern",
        "researchAction",
        "scoring",
        "allowedPaths",
        "touchedPaths",
        "changedPaths",
        "evalFile",
        "deliverablesFile",
        "objective",
        "optimization",
        "scoreDrivenArtifactSearch",
        "optimizeCandidateSearch",
        "candidateGeneration",
        "proofObjective",
        "maintenance",
        "reviewProfile",
        "reviewTier",
        "reviewRoles",
        "patternHints",
        "researchCritic",
        "researchDistillation",
        "criticType",
        "evaluatorExploitReview",
        "parentOptimizeIssue",
        "laneId",
        "laneRole",
        "optimizeLane",
        "humanDecisionRequired",
        "requiresHumanJudgment",
        "planningRequired",
        "successCriteriaClear",
        "status",
    ]
    orch_subset = {
        "activeProject": orch.get("activeProject"),
        "authorizedEpic": orch.get("authorizedEpic"),
        "authorizationFrozenAt": orch.get("authorizationFrozenAt"),
        "approvedEpicIds": orch.get("approvedEpicIds"),
        "approvedIssueIds": orch.get("approvedIssueIds"),
    }
    wf_subset = {
        "research": {
            "parallel": _as_dict(_as_dict(wf.get("research")).get("parallel")),
            "fanout_heuristics": _as_dict(_as_dict(wf.get("research")).get("fanout_heuristics")),
        }
    }
    program = _program(research_ctx)
    research_subset = {
        "enabled": research_ctx.get("enabled"),
        "project": research_ctx.get("project"),
        "program": {
            "research": _research_cfg(program),
            "evaluation": _evaluation_cfg(program),
            "promotion": _promotion_cfg(program),
        },
        "state": {
            "phase": _state(research_ctx).get("phase"),
            "truthStatus": _state(research_ctx).get("truthStatus"),
            "truth": _state(research_ctx).get("truth"),
            "flatline": _state(research_ctx).get("flatline"),
            "distillation": _state(research_ctx).get("distillation"),
            "evaluatorExploitReview": _state(research_ctx).get("evaluatorExploitReview"),
            "deadline": _state(research_ctx).get("deadline"),
            "truthManager": _state(research_ctx).get("truthManager"),
        },
        "truthManager": research_ctx.get("truthManager"),
        "budget": research_ctx.get("budget"),
        "deadlinePassed": research_ctx.get("deadlinePassed"),
        "withinFreezeWindow": research_ctx.get("withinFreezeWindow"),
        "consolidationOnly": research_ctx.get("consolidationOnly"),
        "laneIsolationReady": research_ctx.get("laneIsolationReady"),
    }
    parent_id = _non_empty_str(issue.get("id")) or _non_empty_str(issue.get("parentOptimizeIssue"))
    related_issues = []
    if parent_id:
        for iid, candidate in sorted(issues_by_id.items()):
            if iid == parent_id or str(candidate.get("parentOptimizeIssue") or "") == parent_id or str(candidate.get("id") or "").startswith(parent_id + "-"):
                related_issues.append(
                    {
                        "id": candidate.get("id"),
                        "state": candidate.get("state"),
                        "workerMode": candidate.get("workerMode"),
                        "executionPattern": candidate.get("executionPattern"),
                        "researchAction": candidate.get("researchAction"),
                        "laneRole": candidate.get("laneRole"),
                        "flatlineEntropyStep": candidate.get("flatlineEntropyStep"),
                        "researchCritic": candidate.get("researchCritic"),
                        "researchDistillation": candidate.get("researchDistillation"),
                        "evaluatorExploitReview": candidate.get("evaluatorExploitReview"),
                    }
                )
    return {
        "routerVersion": ROUTER_VERSION,
        "issue": {key: deepcopy(issue.get(key)) for key in issue_keys if key in issue},
        "orch": orch_subset,
        "wf": wf_subset,
        "research": research_subset,
        "relatedIssues": related_issues,
        "signals": signals,
    }


def _direct_enforcement_patch_allowed(
    issue: dict[str, Any],
    *,
    selected_pattern: str,
    status: str,
    helper_action: dict[str, Any],
    candidate_generation_suppressed: bool,
) -> bool:
    if str(issue.get("state") or "") not in {"Todo", "Rework"}:
        return False
    if str(status).startswith("blocked"):
        return False
    if selected_pattern == "flatline_entropy":
        return False
    helper_issue_id = _non_empty_str(helper_action.get("issueId"))
    issue_id = _non_empty_str(issue.get("id"))
    if candidate_generation_suppressed:
        if helper_issue_id and helper_issue_id != issue_id:
            return False
        if selected_pattern == "finalization":
            return str(issue.get("researchAction") or "") in {"final_validation", "submission_bundle"}
        return False
    return True


def derive_execution_pattern(
    issue: dict[str, Any],
    *,
    orch: dict[str, Any],
    wf: dict[str, Any],
    research_ctx: dict[str, Any],
    issues_by_id: dict[str, dict[str, Any]],
    enforcement_enabled: bool = False,
) -> dict[str, Any]:
    """Return a pure routing decision for an issue.

    In shadow mode the returned issue patch contains only ``patternDecision``.
    When ``enforcement_enabled`` is true, safe direct-dispatch decisions for
    Todo/Rework issues also include top-level ``workerMode``/``executionPattern``
    fields for the orchestrator to persist immediately before dispatch. Safety
    holds and helper decisions stay metadata-only so parent optimize contracts are
    not rewritten into their helper worker mode.
    """

    issue = deepcopy(issue) if isinstance(issue, dict) else {}
    orch = deepcopy(orch) if isinstance(orch, dict) else {}
    wf = deepcopy(wf) if isinstance(wf, dict) else {}
    research_ctx = deepcopy(research_ctx) if isinstance(research_ctx, dict) else {}
    issues_by_id = deepcopy(issues_by_id) if isinstance(issues_by_id, dict) else {}

    signals = _extract_signals(issue, orch=orch, wf=wf, research_ctx=research_ctx, issues_by_id=issues_by_id)
    _finalize_comparable_contract(signals)

    worker_mode = str(issue.get("workerMode") or "code")
    existing_pattern = signals.get("existingExecutionPattern")
    parent_issue = _parent_optimize_issue(issue, issues_by_id)
    parent_id = _non_empty_str(parent_issue.get("id")) or _non_empty_str(issue.get("id"))
    frozen_window = bool(signals.get("frozenWindow"))
    approved_ids = set(signals.get("approvedHelperIds") or [])

    base_status = "enforced" if enforcement_enabled else "shadow_only"
    selected_pattern = "code"
    selected_worker_mode = "code"
    status = base_status
    reason = "Defaulted to Wiggum/Ralph code execution."
    alternatives_rejected: list[str] = []
    helper_action = _helper_action()
    candidate_generation_suppressed = False

    maintenance_kind = _as_dict(issue.get("maintenance")).get("kind")

    if maintenance_kind == "harness_candidate_trial" or existing_pattern == "harness_candidate_trial":
        selected_pattern = "harness_candidate_trial"
        selected_worker_mode = "bridge_research"
        reason = "Harness candidate trials are replay-backed bridge_research maintenance, never optimize search."
        alternatives_rejected.append("workerMode=optimize: harness candidate trials cannot be candidate search")
        if worker_mode == "optimize":
            status = "blocked_conflict"
    elif worker_mode == "pipeline":
        selected_pattern = "pipeline"
        selected_worker_mode = "pipeline"
        reason = "Pipeline worker mode uses the resumable/idempotent pipeline pattern."
    elif signals.get("unclearOrHumanJudgment"):
        selected_pattern = "critic" if worker_mode == "bridge_research" or issue.get("researchCritic") else "research_report"
        selected_worker_mode = WORKER_MODE_BY_PATTERN[selected_pattern]
        status = "blocked_human_judgment"
        reason = "Issue has unresolved human judgment, planning, or unclear success criteria; do not continue a blind Wiggum loop."
        alternatives_rejected.append("code: Wiggum/Ralph requires a stable objective and written contract")
        alternatives_rejected.append("optimize: subjective/unresolved human judgment is not fixed evaluator search")
    elif signals.get("touchesFrameworkOrRuntime"):
        selected_pattern = "scored_code" if signals.get("hasScoringBlock") else "code"
        selected_worker_mode = "code"
        if selected_pattern == "scored_code":
            reason = "Measurable framework/code work uses Wiggum/Ralph with score evidence, not optimize search."
            alternatives_rejected.append("optimize: scoring block on engineering work is scored_code, not candidate search")
        else:
            reason = "Framework/runtime/code work stays in the Wiggum/Ralph code loop."
            alternatives_rejected.append("optimize: framework/runtime/code changes cannot be candidate search")
    elif worker_mode == "optimize" or signals.get("explicitScoreDrivenArtifactSearch"):
        action = str(issue.get("researchAction") or "candidate_generation")
        if action in {"final_validation", "submission_bundle"} or signals.get("withinSubmissionFreeze") or signals.get("deadlinePassed"):
            selected_pattern = "finalization"
            selected_worker_mode = "optimize"
            reason = "Deadline/submission freeze/consolidation allows final validation and packaging only; fresh candidates are suppressed."
            candidate_generation_suppressed = True
            alternatives_rejected.append("optimize_fanout: finalization/freeze forbids fresh candidate generation")
            if parent_id and action in {"final_validation", "submission_bundle"}:
                helper_action = _helper_action("finalization", _non_empty_str(issue.get("id")) or None, frozen_window=frozen_window, approved_ids=approved_ids, safety_hold=True)
        elif signals.get("truthStatus") == "unavailable" or signals.get("researchPhase") == "blocked_truth_unavailable":
            repair_needed, failure_source, failure_detail = _truth_repair_needed(research_ctx)
            candidate_generation_suppressed = True
            alternatives_rejected.append("optimize: no-truth/no-search suppresses candidate generation")
            if repair_needed and parent_id:
                selected_pattern = "truth_repair"
                selected_worker_mode = "code"
                helper_id = f"{parent_id}-truth-repair"
                helper_action = _helper_action("truth_repair", helper_id, frozen_window=frozen_window, approved_ids=approved_ids, safety_hold=True)
                if frozen_window and not helper_action["dispatchAllowed"]:
                    status = "blocked_unapproved_helper"
                reason = "Truth path is unavailable from evaluator/validator/runtime failure; route to engineering repair and keep search suppressed."
                if failure_source or failure_detail:
                    reason += f" Last failure: {failure_source or 'unknown'} / {failure_detail or 'unknown'}."
            else:
                selected_pattern = "critic"
                selected_worker_mode = "bridge_research"
                status = "blocked_missing_prerequisite"
                reason = "Truth path is unavailable and no deterministic repair helper is in scope; suppress candidate generation."
        elif signals.get("evaluatorExploitReviewPending"):
            selected_pattern = "evaluator_exploit_review"
            selected_worker_mode = "bridge_research"
            candidate_generation_suppressed = True
            helper_id = _evaluator_review_issue_id(parent_id, research_ctx) if parent_id else None
            helper_action = _helper_action("evaluator_exploit_review", helper_id, frozen_window=frozen_window, approved_ids=approved_ids, safety_hold=True)
            if frozen_window and not helper_action["dispatchAllowed"]:
                status = "blocked_unapproved_helper"
            reason = "Evaluator-exploit review is pending; pause candidate generation for a bridge_research critic."
            alternatives_rejected.append("optimize: evaluator-exploit review hold precedes more search")
        elif signals.get("livingDistillationPending"):
            selected_pattern = "critic"
            selected_worker_mode = "bridge_research"
            candidate_generation_suppressed = True
            helper_id = _distillation_issue_id(parent_id, research_ctx) if parent_id else None
            helper_action = _helper_action("living_distillation", helper_id, frozen_window=frozen_window, approved_ids=approved_ids, safety_hold=True)
            if frozen_window and not helper_action["dispatchAllowed"]:
                status = "blocked_unapproved_helper"
            reason = "Living distillation is pending; pause candidate generation until transferable lessons are recorded."
            alternatives_rejected.append("optimize: distillation hold precedes more candidate generation")
        elif signals.get("flatlineActive"):
            selected_pattern = "flatline_entropy"
            candidate_generation_suppressed = True
            step = _first_incomplete_flatline_step(parent_id, research_ctx, issues_by_id) if parent_id else {}
            step_kind = step.get("kind")
            selected_worker_mode = "optimize" if step_kind == "optimize" else "bridge_research"
            helper_id = step.get("issueId") if isinstance(step, dict) else None
            helper_action = _helper_action("flatline_entropy_step", helper_id, frozen_window=frozen_window, approved_ids=approved_ids, safety_hold=True)
            if frozen_window and helper_id and not helper_action["dispatchAllowed"]:
                status = "blocked_unapproved_helper"
            reason = "Flatline entropy playbook is active; run the first incomplete ordered entropy step before local candidate tweaking."
            if step.get("stepId"):
                reason += f" Next step: {step.get('stepId')} ({step.get('index')}/{step.get('total')})."
            alternatives_rejected.append("optimize_single: flatline hold requires entropy sequence first")
            alternatives_rejected.append("optimize_fanout: flatline hold requires entropy sequence first")
        elif not _candidate_generation_action(issue):
            selected_pattern = "research_report"
            selected_worker_mode = "research"
            reason = f"Optimize worker action {action!r} is not candidate generation or finalization; fail closed to setup/evidence routing."
            status = "blocked_missing_prerequisite"
            alternatives_rejected.append("optimize: non-candidate action lacks a canonical optimize search pattern")
        else:
            missing = _optimize_prerequisites(signals)
            if missing:
                selected_pattern = "research_report"
                selected_worker_mode = "research"
                status = "blocked_missing_prerequisite"
                candidate_generation_suppressed = True
                reason = "Optimize prerequisites failed closed: " + "; ".join(missing[:6])
                alternatives_rejected.append("optimize_single: " + "; ".join(missing))
                alternatives_rejected.append("optimize_fanout: " + "; ".join(missing))
            else:
                research_cfg = _as_dict(wf.get("research"))
                fanout_heuristics = _as_dict(research_cfg.get("fanout_heuristics"))
                try:
                    min_budget = float(fanout_heuristics.get("min_budget_remaining_pct_for_fanout") or 30)
                except Exception:
                    min_budget = 30.0
                budget_ok = signals.get("budgetRemainingPct") is None or float(signals.get("budgetRemainingPct")) >= min_budget
                fanout_ok = bool(
                    signals.get("autoFanoutEnabled")
                    and signals.get("projectFanoutEnabled")
                    and signals.get("laneIsolationReady")
                    and not signals.get("proxyTruthMismatch")
                    and not signals.get("evaluatorThroughputBottleneck")
                    and budget_ok
                )
                if fanout_ok:
                    selected_pattern = "optimize_fanout"
                    selected_worker_mode = "optimize"
                    reason = "Comparable optimize contract is ready and fanout is enabled with lane isolation, budget, truth, and throughput."
                    alternatives_rejected.append("optimize_single: fanout is safe and should improve information gain")
                else:
                    selected_pattern = "optimize_single"
                    selected_worker_mode = "optimize"
                    reason = "Comparable optimize contract is ready, but fanout prerequisites are not all satisfied; use one lane."
                    if not signals.get("autoFanoutEnabled") or not signals.get("projectFanoutEnabled"):
                        alternatives_rejected.append("optimize_fanout: auto-fanout disabled")
                    if not signals.get("laneIsolationReady"):
                        alternatives_rejected.append("optimize_fanout: lane isolation not ready")
                    if signals.get("proxyTruthMismatch"):
                        alternatives_rejected.append("optimize_fanout: proxy/truth mismatch requires calibration or critique")
                    if signals.get("evaluatorThroughputBottleneck"):
                        alternatives_rejected.append("optimize_fanout: evaluator throughput is the bottleneck")
                    if not budget_ok:
                        alternatives_rejected.append("optimize_fanout: budget remaining is below fanout threshold")
    elif worker_mode == "code":
        selected_pattern = "scored_code" if signals.get("hasScoringBlock") else "code"
        selected_worker_mode = "code"
        if selected_pattern == "scored_code":
            reason = "Measurable engineering work uses Wiggum/Ralph with score evidence, not optimize search."
            alternatives_rejected.append("optimize: scoring block on engineering work is scored_code, not candidate search")
        else:
            reason = "Code work stays in the Wiggum/Ralph code loop."
            alternatives_rejected.append("optimize: no explicit score-driven artifact-search declaration")
    elif worker_mode in {"research", "bridge_research"}:
        selected_pattern = _research_or_critic_pattern(issue)
        selected_worker_mode = WORKER_MODE_BY_PATTERN[selected_pattern]
        reason = "Research, evidence, reporting, distillation, and outer-loop critique use analyst/critic patterns, not optimize candidate generation."
        alternatives_rejected.append("optimize: research/critic worker modes do not own candidate generation")
    else:
        safe_existing = existing_pattern if existing_pattern in CANONICAL_EXECUTION_PATTERNS else None
        if safe_existing and not safe_existing.startswith("optimize"):
            selected_pattern = safe_existing
            selected_worker_mode = WORKER_MODE_BY_PATTERN[selected_pattern]
            reason = "Preserved explicit safe non-optimize execution pattern as a shadow decision."
        else:
            selected_pattern = "research_report"
            selected_worker_mode = "research"
            status = "blocked_missing_prerequisite"
            reason = "Ambiguous worker mode failed closed to research/reporting rather than guessing into optimize."
            alternatives_rejected.append("optimize: ambiguous worker mode cannot be upgraded to candidate search")

    if selected_pattern.startswith("optimize") and signals.get("touchesFrameworkOrRuntime"):
        selected_pattern = "scored_code" if signals.get("hasScoringBlock") else "code"
        selected_worker_mode = "code"
        candidate_generation_suppressed = False
        status = base_status
        reason = "Framework/runtime/control-plane paths cannot route to optimize; coerced to code/scored_code pattern."
        alternatives_rejected.append("optimize: framework/runtime/control-plane path boundary")

    status = _apply_pattern_hints(issue, selected_pattern, status, alternatives_rejected, signals)

    input_fingerprint = _sha256_obj(
        _stable_input_subset(
            issue,
            orch=orch,
            wf=wf,
            research_ctx=research_ctx,
            issues_by_id=issues_by_id,
            signals=signals,
        )
    )
    decision_id = _sha256_obj(
        {
            "routerVersion": ROUTER_VERSION,
            "selectedPattern": selected_pattern,
            "selectedWorkerMode": selected_worker_mode,
            "status": status,
            "inputFingerprint": input_fingerprint,
            "helperAction": helper_action,
            "candidateGenerationSuppressed": candidate_generation_suppressed,
        }
    )

    pattern_decision = {
        "schemaVersion": 1,
        "routerVersion": ROUTER_VERSION,
        "selectedBy": "orchestrator",
        "selectedAt": None,
        "selectedPattern": selected_pattern,
        "selectedWorkerMode": selected_worker_mode,
        "inputFingerprint": input_fingerprint,
        "decisionId": decision_id,
        "status": status,
        "reason": reason,
        "signals": signals,
        "helperAction": helper_action,
        "candidateGenerationSuppressed": bool(candidate_generation_suppressed),
        "alternativesRejected": alternatives_rejected,
        "reevaluateOn": REEVALUATE_ON,
    }

    issue_patch: dict[str, Any] = {"patternDecision": pattern_decision}
    if enforcement_enabled and _direct_enforcement_patch_allowed(
        issue,
        selected_pattern=selected_pattern,
        status=status,
        helper_action=helper_action,
        candidate_generation_suppressed=bool(candidate_generation_suppressed),
    ):
        issue_patch["workerMode"] = selected_worker_mode
        issue_patch["executionPattern"] = selected_pattern
        if selected_worker_mode == "optimize" and selected_pattern in {"optimize_single", "optimize_fanout"} and not issue.get("researchAction"):
            issue_patch["researchAction"] = "candidate_generation"

    return {
        "schemaVersion": 1,
        "routerVersion": ROUTER_VERSION,
        "selectedPattern": selected_pattern,
        "selectedWorkerMode": selected_worker_mode,
        "status": status,
        "reason": reason,
        "signals": signals,
        "helperAction": helper_action,
        "candidateGenerationSuppressed": bool(candidate_generation_suppressed),
        "alternativesRejected": alternatives_rejected,
        "inputFingerprint": input_fingerprint,
        "decisionId": decision_id,
        "issuePatch": issue_patch,
    }


__all__ = [
    "CANONICAL_EXECUTION_PATTERNS",
    "EXECUTION_PATTERN_ALIASES",
    "ROUTER_VERSION",
    "canonicalize_execution_pattern",
    "derive_execution_pattern",
]
