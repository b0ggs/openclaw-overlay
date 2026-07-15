#!/usr/bin/env python3
"""Tests for the shadow execution-pattern router."""

from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from scripts.execution_pattern_router import canonicalize_execution_pattern, derive_execution_pattern

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


VALID_PROGRAM = {
    "research": {
        "artifact_root": "src",
        "canonical_artifacts": ["src/champion.py"],
        "fanout": {"auto": True, "require_lane_isolation": True},
    },
    "evaluation": {
        "metric": "score",
        "direction": "maximize",
        "evaluator_command": "python eval.py",
        "validator_command": "python validate.py",
        "seeds": {"quick": 1, "confirm": 4, "promotion": 8, "final": 16},
        "budget": {"total_evaluations": 100, "total_tokens": 1000000},
    },
    "promotion": {"validator_required": True, "consolidation_threshold_pct": 20},
}


BASE_WF = {
    "research": {
        "parallel": {"auto_fanout": True, "default_workers": 2, "max_workers_per_project": 4},
        "fanout_heuristics": {
            "max_eval_latency_seconds_for_fanout": 120,
            "min_budget_remaining_pct_for_fanout": 30,
        },
    }
}


_DEFAULT_PROGRAM = object()


def research_ctx(*, program=_DEFAULT_PROGRAM, phase="search", truth="available", candidate_allowed=True, **overrides):
    program = VALID_PROGRAM if program is _DEFAULT_PROGRAM else program
    state = {
        "phase": phase,
        "truthStatus": truth,
        "truth": {},
        "flatline": {},
        "distillation": {},
        "evaluatorExploitReview": {"status": "complete"},
    }
    truth_manager = {
        "candidateGenerationAllowed": candidate_allowed,
        "proxyTruthMismatch": False,
        "evaluatorLatencySeconds": 30,
    }
    ctx = {
        "enabled": bool(program),
        "project": "optproj",
        "program": program or {},
        "state": state,
        "truthManager": truth_manager,
        "budget": {"remainingEvaluationsPct": 80},
        "deadlinePassed": False,
        "withinFreezeWindow": False,
        "consolidationOnly": False,
        "laneIsolationReady": True,
    }
    for key, value in overrides.items():
        if key == "state":
            ctx["state"].update(value)
        elif key == "truthManager":
            ctx["truthManager"].update(value)
        else:
            ctx[key] = value
    return ctx


def optimize_issue(**overrides):
    issue = {
        "id": "opt-parent",
        "kind": "task",
        "project": "optproj",
        "title": "Search champion",
        "state": "Todo",
        "workerMode": "optimize",
        "researchAction": "candidate_generation",
        "allowedPaths": ["src/champion.py"],
        "proofObjective": "Improve the fixed evaluator metric.",
    }
    issue.update(overrides)
    return issue


def decision(issue, *, orch=None, wf=None, ctx=None, issues_by_id=None):
    return derive_execution_pattern(
        issue,
        orch=orch or {},
        wf=BASE_WF if wf is None else wf,
        research_ctx=research_ctx() if ctx is None else ctx,
        issues_by_id=issues_by_id or {issue.get("id", "issue"): issue},
    )


class ExecutionPatternRouterUnitTests(unittest.TestCase):
    def test_code_issue_without_scoring_routes_code(self) -> None:
        d = decision({"id": "code-1", "kind": "task", "project": "app", "state": "Todo", "workerMode": "code", "proofObjective": "Implement X"})
        self.assertEqual(d["selectedPattern"], "code")
        self.assertEqual(d["selectedWorkerMode"], "code")

    def test_code_issue_with_scoring_routes_scored_code_not_optimize(self) -> None:
        d = decision(
            {
                "id": "code-score",
                "kind": "task",
                "project": "app",
                "state": "Todo",
                "workerMode": "code",
                "proofObjective": "Reduce latency",
                "scoring": {"metric": "latency_ms", "direction": "minimize", "command": "python bench.py"},
            }
        )
        self.assertEqual(d["selectedPattern"], "scored_code")
        self.assertNotIn("optimize", d["selectedPattern"])

    def test_framework_runtime_paths_never_optimize(self) -> None:
        d = decision(optimize_issue(project="(framework)", allowedPaths=["scripts/orchestrator-tick.py"], scoring={"metric": "coverage"}))
        self.assertEqual(d["selectedPattern"], "scored_code")
        self.assertEqual(d["selectedWorkerMode"], "code")

    def test_unclear_success_criteria_routes_away_from_blind_wiggum(self) -> None:
        d = decision({"id": "unclear", "kind": "task", "project": "app", "state": "Todo", "workerMode": "code", "successCriteriaClear": False})
        self.assertIn(d["selectedPattern"], {"research_report", "critic"})
        self.assertEqual(d["status"], "blocked_human_judgment")

    def test_human_judgment_design_routes_away_from_blind_wiggum(self) -> None:
        d = decision({"id": "policy", "kind": "task", "project": "app", "state": "Todo", "workerMode": "code", "humanDecisionRequired": True})
        self.assertEqual(d["selectedPattern"], "research_report")
        self.assertNotEqual(d["selectedPattern"], "code")

    def test_optimize_without_research_program_fails_closed(self) -> None:
        d = decision(optimize_issue(), ctx=research_ctx(program=None))
        self.assertEqual(d["status"], "blocked_missing_prerequisite")
        self.assertNotIn(d["selectedPattern"], {"optimize_single", "optimize_fanout"})

    def test_optimize_with_mutable_evaluator_surface_fails_closed(self) -> None:
        d = decision(optimize_issue(allowedPaths=["evaluator.py"]))
        self.assertEqual(d["status"], "blocked_missing_prerequisite")
        self.assertFalse(d["signals"]["immutableEvaluatorSurface"])

    def test_optimize_lane_with_mutable_evaluator_surface_fails_closed(self) -> None:
        d = decision(optimize_issue(optimizeLane=True, laneId="lane-a", allowedPaths=["evaluator.py"]))
        self.assertEqual(d["status"], "blocked_missing_prerequisite")
        self.assertNotIn(d["selectedPattern"], {"optimize_single", "optimize_fanout"})
        self.assertFalse(d["signals"]["immutableEvaluatorSurface"])

    def test_optimize_lane_with_missing_allowed_paths_fails_closed(self) -> None:
        issue = optimize_issue(optimizeLane=True, laneId="lane-a")
        issue.pop("allowedPaths")
        d = decision(issue)
        self.assertEqual(d["status"], "blocked_missing_prerequisite")
        self.assertFalse(d["signals"]["editableSurfaceEnforced"])

    def test_broad_artifact_root_allowed_path_is_still_not_evaluator_mutable(self) -> None:
        d = decision(optimize_issue(allowedPaths=["src"]))
        self.assertEqual(d["selectedPattern"], "optimize_fanout")
        self.assertTrue(d["signals"]["editableSurfaceEnforced"])
        self.assertTrue(d["signals"]["immutableEvaluatorSurface"])

    def test_broad_allowed_path_outside_artifact_root_fails_closed(self) -> None:
        d = decision(optimize_issue(allowedPaths=["."]))
        self.assertEqual(d["status"], "blocked_missing_prerequisite")
        self.assertFalse(d["signals"]["editableSurfaceEnforced"])

    def test_optimize_without_fixed_candidate_budget_fails_closed(self) -> None:
        program = json.loads(json.dumps(VALID_PROGRAM))
        program["evaluation"]["budget"] = {}
        d = decision(optimize_issue(), ctx=research_ctx(program=program))
        self.assertEqual(d["status"], "blocked_missing_prerequisite")
        self.assertFalse(d["signals"]["fixedCandidateBudget"])

    def test_optimize_with_ambiguous_editable_surface_fails_closed(self) -> None:
        issue = optimize_issue()
        issue.pop("allowedPaths")
        d = decision(issue)
        self.assertEqual(d["status"], "blocked_missing_prerequisite")
        self.assertFalse(d["signals"]["editableSurfaceEnforced"])

    def test_optimize_with_non_comparable_metric_fails_closed(self) -> None:
        program = json.loads(json.dumps(VALID_PROGRAM))
        program["evaluation"]["direction"] = ""
        d = decision(optimize_issue(), ctx=research_ctx(program=program))
        self.assertEqual(d["status"], "blocked_missing_prerequisite")
        self.assertFalse(d["signals"]["metricComparable"])

    def test_optimize_ready_routes_single_when_fanout_disabled(self) -> None:
        wf = json.loads(json.dumps(BASE_WF))
        wf["research"]["parallel"]["auto_fanout"] = False
        d = decision(optimize_issue(), wf=wf)
        self.assertEqual(d["selectedPattern"], "optimize_single")

    def test_optimize_ready_routes_fanout_when_safe(self) -> None:
        d = decision(optimize_issue())
        self.assertEqual(d["selectedPattern"], "optimize_fanout")

    def test_proxy_truth_mismatch_avoids_fanout(self) -> None:
        d = decision(optimize_issue(), ctx=research_ctx(truthManager={"proxyTruthMismatch": True}))
        self.assertEqual(d["selectedPattern"], "optimize_single")
        self.assertIn("proxy/truth mismatch", " ".join(d["alternativesRejected"]))

    def test_evaluator_throughput_bottleneck_avoids_fanout(self) -> None:
        d = decision(optimize_issue(), ctx=research_ctx(truthManager={"evaluatorLatencySeconds": 999}))
        self.assertEqual(d["selectedPattern"], "optimize_single")
        self.assertIn("throughput", " ".join(d["alternativesRejected"]))

    def test_truth_outage_suppresses_candidate_generation_and_routes_repair(self) -> None:
        ctx = research_ctx(
            phase="blocked_truth_unavailable",
            truth="unavailable",
            candidate_allowed=False,
            state={"truth": {"lastFailureSource": "evaluator", "lastFailureDetail": "timeout"}},
        )
        d = decision(optimize_issue(), ctx=ctx)
        self.assertEqual(d["selectedPattern"], "truth_repair")
        self.assertTrue(d["candidateGenerationSuppressed"])

    def test_submission_freeze_suppresses_candidate_generation(self) -> None:
        d = decision(optimize_issue(researchAction="candidate_generation"), ctx=research_ctx(phase="submission_freeze", withinFreezeWindow=True))
        self.assertEqual(d["selectedPattern"], "finalization")
        self.assertTrue(d["candidateGenerationSuppressed"])

    def test_evaluator_exploit_review_suppresses_candidate_generation(self) -> None:
        ctx = research_ctx(state={"evaluatorExploitReview": {"status": "pending", "pendingReason": "mid_budget", "sequence": 2}})
        d = decision(optimize_issue(), ctx=ctx)
        self.assertEqual(d["selectedPattern"], "evaluator_exploit_review")
        self.assertEqual(d["helperAction"]["issueId"], "opt-parent-evaluator-exploit-mid-budget-2")
        self.assertTrue(d["candidateGenerationSuppressed"])

    def test_living_distillation_suppresses_candidate_generation(self) -> None:
        ctx = research_ctx(state={"distillation": {"pendingEvents": [{"event": "champion_replacement"}], "sequence": 3}})
        d = decision(optimize_issue(), ctx=ctx)
        self.assertEqual(d["selectedPattern"], "critic")
        self.assertEqual(d["helperAction"]["issueId"], "opt-parent-distillation-3")
        self.assertTrue(d["candidateGenerationSuppressed"])

    def test_flatline_selects_first_incomplete_entropy_step(self) -> None:
        ctx = research_ctx(
            phase="blocked_flatline",
            state={
                "flatline": {
                    "playbook": {
                        "active": True,
                        "runSeq": 1,
                        "steps": [
                            {"id": "literature_refresh", "kind": "bridge_research"},
                            {"id": "alt_framing_critic", "kind": "bridge_research"},
                        ],
                        "stepStatus": {},
                    }
                }
            },
        )
        d = decision(optimize_issue(), ctx=ctx)
        self.assertEqual(d["selectedPattern"], "flatline_entropy")
        self.assertEqual(d["helperAction"]["issueId"], "opt-parent-flatline-literature")
        self.assertTrue(d["candidateGenerationSuppressed"])

    def test_harness_candidate_trial_with_optimize_is_blocked(self) -> None:
        d = decision(optimize_issue(maintenance={"kind": "harness_candidate_trial"}, executionPattern="harness_candidate_trial"))
        self.assertEqual(d["selectedPattern"], "harness_candidate_trial")
        self.assertEqual(d["status"], "blocked_conflict")
        self.assertEqual(d["selectedWorkerMode"], "bridge_research")

    def test_frozen_window_unapproved_helper_is_blocked_and_suppresses_search(self) -> None:
        orch = {"authorizationFrozenAt": "2026-05-01T00:00:00Z", "approvedIssueIds": ["opt-parent"]}
        ctx = research_ctx(state={"evaluatorExploitReview": {"status": "pending", "pendingReason": "mid_budget", "sequence": 1}})
        d = decision(optimize_issue(), orch=orch, ctx=ctx)
        self.assertEqual(d["status"], "blocked_unapproved_helper")
        self.assertFalse(d["helperAction"]["dispatchAllowed"])
        self.assertTrue(d["candidateGenerationSuppressed"])

    def test_frozen_window_predeclared_helper_can_dispatch(self) -> None:
        helper_id = "opt-parent-evaluator-exploit-mid-budget-1"
        orch = {"authorizationFrozenAt": "2026-05-01T00:00:00Z", "approvedIssueIds": ["opt-parent", helper_id]}
        ctx = research_ctx(state={"evaluatorExploitReview": {"status": "pending", "pendingReason": "mid_budget", "sequence": 1}})
        d = decision(optimize_issue(), orch=orch, ctx=ctx)
        self.assertEqual(d["status"], "shadow_only")
        self.assertTrue(d["helperAction"]["dispatchAllowed"])

    def test_pattern_hints_requesting_optimize_cannot_override_no_truth(self) -> None:
        ctx = research_ctx(
            phase="blocked_truth_unavailable",
            truth="unavailable",
            candidate_allowed=False,
            state={"truth": {"lastFailureSource": "evaluator", "lastFailureDetail": "timeout"}},
        )
        d = decision(optimize_issue(patternHints={"allowedPatterns": ["optimize_fanout"]}), ctx=ctx)
        self.assertNotIn(d["selectedPattern"], {"optimize_single", "optimize_fanout"})
        self.assertEqual(d["status"], "blocked_pattern_hint_conflict")

    def test_pattern_hints_forbidding_only_safe_pattern_blocks(self) -> None:
        d = decision({"id": "code", "kind": "task", "project": "app", "state": "Todo", "workerMode": "code", "proofObjective": "Do it", "patternHints": {"forbiddenPatterns": ["code"]}})
        self.assertEqual(d["selectedPattern"], "code")
        self.assertEqual(d["status"], "blocked_pattern_hint_conflict")

    def test_preexisting_unsafe_execution_pattern_cannot_override_safety(self) -> None:
        d = decision({"id": "unsafe", "kind": "task", "project": "(framework)", "state": "Todo", "workerMode": "code", "executionPattern": "optimize_fanout", "allowedPaths": ["scripts/foo.py"]})
        self.assertEqual(d["selectedPattern"], "code")

    def test_repeated_wiggum_failure_does_not_become_optimize(self) -> None:
        d = decision({"id": "retry", "kind": "task", "project": "app", "state": "Rework", "workerMode": "code", "executionPattern": "optimize_fanout", "status": {"retryCount": 4}})
        self.assertEqual(d["selectedPattern"], "code")
        self.assertNotIn("optimize", d["selectedPattern"])

    def test_bridge_critic_alias_normalizes_to_critic(self) -> None:
        self.assertEqual(canonicalize_execution_pattern("bridge_critic"), "critic")
        d = decision({"id": "crit", "kind": "task", "project": "app", "state": "Todo", "workerMode": "bridge_research", "executionPattern": "bridge_critic"})
        self.assertEqual(d["selectedPattern"], "critic")

    def test_fingerprint_and_decision_id_are_stable(self) -> None:
        issue = optimize_issue()
        d1 = decision(issue)
        d2 = decision(issue)
        self.assertEqual(d1["inputFingerprint"], d2["inputFingerprint"])
        self.assertEqual(d1["decisionId"], d2["decisionId"])


class ExecutionPatternRouterIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "workspace"
        self.state_dir = self.root / "state"
        (self.state_dir / "issues").mkdir(parents=True, exist_ok=True)
        (self.root / "scripts").mkdir(parents=True, exist_ok=True)
        self.write_workflow()
        self.tick = load_module(f"tick_pattern_router_{id(self)}", REPO_ROOT / "scripts" / "orchestrator-tick.py")
        self.tick.ROOT = self.root
        self.tick.SCRIPTS_DIR = self.root / "scripts"
        self.tick.STATE_DIR = self.state_dir
        self.tick.FROZEN_AUTH_PATH = self.state_dir / "frozen-authorization.json"
        self.tick.QUARANTINE_RUNTIME_HELPER = self.root / "scripts" / "quarantine-runtime.py"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_workflow(self, *, router_mode: str | None = None, auto_fanout: bool = True) -> None:
        router_block = f"execution_pattern_router:\n  mode: {router_mode}\n" if router_mode else ""
        auto_fanout_value = "true" if auto_fanout else "false"
        (self.root / "WORKFLOW.md").write_text(
            f"""---
agents:
  max_concurrent: 1
workspace:
  root: ~/.openclaw/worktrees
{router_block}research:
  parallel:
    auto_fanout: {auto_fanout_value}
    default_workers: 2
    max_workers_per_project: 4
  fanout_heuristics:
    max_eval_latency_seconds_for_fanout: 120
    min_budget_remaining_pct_for_fanout: 30
autonomy:
  default_max_required_children: 20
  allow_project_scope_blanket_go: true
  max_unique_issues_per_window: 30
  max_total_dispatches_per_window: 100
quarantine:
  multi_worker:
    enabled: false
self_improvement:
  retrospective:
    enabled: false
---
workflow
""",
            encoding="utf-8",
        )

    def write_json(self, rel: str, payload: dict) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def write_issue(self, issue_id: str, payload: dict) -> None:
        self.write_json(f"state/issues/{issue_id}.json", payload)

    def run_tick(self, *, research_context: dict | None = None):
        dispatches: list[str] = []

        def fake_run(*args, **kwargs):
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        stop_path = Path(self.tmp.name) / "STOP"
        with mock.patch.object(self.tick, "stop_file", return_value=stop_path), \
            mock.patch.object(self.tick.subprocess, "run", side_effect=fake_run), \
            mock.patch.object(self.tick, "render_views", return_value=None), \
            mock.patch.object(self.tick, "dispatch", side_effect=lambda issue_id: dispatches.append(issue_id) or 0):
            if research_context is None:
                with mock.patch.object(self.tick, "apply_research_runtime", return_value={"enabled": False}):
                    rc = self.tick.main()
            else:
                with mock.patch.object(self.tick, "apply_research_runtime", return_value=research_context):
                    rc = self.tick.main()
        return rc, dispatches

    def test_enforced_tick_converts_explicit_artifact_search_to_optimize_dispatch(self) -> None:
        self.write_workflow(router_mode="enforced", auto_fanout=False)
        self.write_json(
            "state/orchestrator.json",
            {
                "schemaVersion": 1,
                "activeProject": "optproj",
                "phase": "executing",
                "authorizedEpic": "epic-opt",
                "approvedEpicIds": [],
                "approvedIssueIds": [],
                "authorizationFrozenAt": None,
                "uniqueIssuesDispatched": 0,
                "totalDispatches": 0,
            },
        )
        self.write_issue("epic-opt", {"id": "epic-opt", "kind": "epic", "project": "optproj", "title": "Epic", "state": "Todo", "children": ["safe-opt"]})
        self.write_issue(
            "safe-opt",
            optimize_issue(
                id="safe-opt",
                parent="epic-opt",
                workerMode="code",
                executionPattern=None,
                objective={"kind": "artifact_search", "optimization": True},
                researchAction="candidate_generation",
            ),
        )

        rc, dispatches = self.run_tick(research_context=research_ctx())
        self.assertEqual(rc, 0)
        self.assertEqual(dispatches, ["safe-opt"])
        issue = json.loads((self.state_dir / "issues" / "safe-opt.json").read_text())
        self.assertEqual(issue["workerMode"], "optimize")
        self.assertEqual(issue["executionPattern"], "optimize_single")
        self.assertEqual(issue["patternDecision"]["status"], "enforced")
        self.assertEqual(issue["patternDecision"]["selectedWorkerMode"], "optimize")
        self.assertEqual(issue["state"], "In Progress")
        ledger = [json.loads(line) for line in (self.state_dir / "orchestrator-decisions.jsonl").read_text().splitlines() if line.strip()]
        self.assertTrue(any(entry.get("decision") == "enforced_execution_pattern" and entry.get("issueId") == "safe-opt" for entry in ledger))

    def test_enforced_tick_keeps_scored_code_on_code_route(self) -> None:
        self.write_workflow(router_mode="enforced")
        self.write_json(
            "state/orchestrator.json",
            {
                "schemaVersion": 1,
                "activeProject": "app",
                "phase": "executing",
                "authorizedEpic": "epic-code",
                "approvedEpicIds": [],
                "approvedIssueIds": [],
                "authorizationFrozenAt": None,
                "uniqueIssuesDispatched": 0,
                "totalDispatches": 0,
            },
        )
        self.write_issue("epic-code", {"id": "epic-code", "kind": "epic", "project": "app", "title": "Epic", "state": "Todo", "children": ["code-score"]})
        self.write_issue(
            "code-score",
            {
                "id": "code-score",
                "kind": "task",
                "project": "app",
                "parent": "epic-code",
                "title": "Reduce latency",
                "state": "Todo",
                "workerMode": "code",
                "proofObjective": "Reduce latency in production code.",
                "allowedPaths": ["src/server.py"],
                "scoring": {"command": "python bench.py", "metric": "latency_ms", "direction": "minimize"},
            },
        )

        rc, dispatches = self.run_tick()
        self.assertEqual(rc, 0)
        self.assertEqual(dispatches, ["code-score"])
        issue = json.loads((self.state_dir / "issues" / "code-score.json").read_text())
        self.assertEqual(issue["workerMode"], "code")
        self.assertEqual(issue["executionPattern"], "scored_code")
        self.assertEqual(issue["patternDecision"]["selectedPattern"], "scored_code")
        self.assertEqual(issue["patternDecision"]["status"], "enforced")
        self.assertEqual(issue["state"], "In Progress")

    def test_enforced_tick_blocks_unsafe_optimize_prerequisites_without_dispatch(self) -> None:
        self.write_workflow(router_mode="enforced", auto_fanout=False)
        self.write_json(
            "state/orchestrator.json",
            {
                "schemaVersion": 1,
                "activeProject": "optproj",
                "phase": "executing",
                "authorizedEpic": "epic-opt",
                "approvedEpicIds": [],
                "approvedIssueIds": [],
                "authorizationFrozenAt": None,
                "uniqueIssuesDispatched": 0,
                "totalDispatches": 0,
            },
        )
        self.write_issue("epic-opt", {"id": "epic-opt", "kind": "epic", "project": "optproj", "title": "Epic", "state": "Todo", "children": ["unsafe-opt"]})
        self.write_issue(
            "unsafe-opt",
            optimize_issue(
                id="unsafe-opt",
                parent="epic-opt",
                workerMode="code",
                objective={"kind": "artifact_search", "optimization": True},
                allowedPaths=[],
            ),
        )

        rc, dispatches = self.run_tick(research_context=research_ctx(program=None))
        self.assertEqual(rc, 0)
        self.assertEqual(dispatches, [])
        issue = json.loads((self.state_dir / "issues" / "unsafe-opt.json").read_text())
        self.assertEqual(issue["state"], "Blocked")
        self.assertEqual(issue["status"]["blockedReason"], "execution_pattern_router:blocked_missing_prerequisite")
        self.assertEqual(issue["patternDecision"]["status"], "blocked_missing_prerequisite")
        self.assertTrue(issue["patternDecision"]["candidateGenerationSuppressed"])
        self.assertNotIn("executionPattern", issue)

    def test_enforced_frozen_window_unapproved_helper_blocks_parent_dispatch(self) -> None:
        self.write_workflow(router_mode="enforced")
        self.write_json(
            "state/orchestrator.json",
            {
                "schemaVersion": 1,
                "activeProject": "optproj",
                "phase": "executing",
                "authorizedEpic": "epic-opt",
                "approvedEpicIds": ["epic-opt"],
                "approvedIssueIds": ["opt-parent"],
                "authorizationFrozenAt": "2026-05-01T00:00:00Z",
                "uniqueIssuesDispatched": 0,
                "totalDispatches": 0,
            },
        )
        self.write_json(
            "state/frozen-authorization.json",
            {
                "project": "optproj",
                "approvedEpicIds": ["epic-opt"],
                "approvedIssueIds": ["opt-parent"],
                "authorizationFrozenAt": "2026-05-01T00:00:00Z",
                "authorizationProject": "optproj",
            },
        )
        self.write_issue("epic-opt", {"id": "epic-opt", "kind": "epic", "project": "optproj", "title": "Epic", "state": "Todo", "children": ["opt-parent"]})
        self.write_issue("opt-parent", optimize_issue(parent="epic-opt"))
        ctx = research_ctx(state={"evaluatorExploitReview": {"status": "pending", "pendingReason": "mid_budget", "sequence": 1}})

        rc, dispatches = self.run_tick(research_context=ctx)
        self.assertEqual(rc, 0)
        self.assertEqual(dispatches, [])
        issue = json.loads((self.state_dir / "issues" / "opt-parent.json").read_text())
        self.assertEqual(issue["state"], "Blocked")
        self.assertEqual(issue["status"]["blockedReason"], "execution_pattern_router:blocked_unapproved_helper")
        self.assertEqual(issue["patternDecision"]["status"], "blocked_unapproved_helper")
        self.assertFalse(issue["patternDecision"]["helperAction"]["dispatchAllowed"])
        self.assertTrue(issue["patternDecision"]["candidateGenerationSuppressed"])
        self.assertNotIn("executionPattern", issue)

    def test_shadow_tick_persists_pattern_decision_dedupes_ledger_and_leaves_contract_untouched(self) -> None:
        workdir = Path(self.tmp.name) / "worker"
        workdir.mkdir()
        contract = workdir / ".issue-contract.md"
        contract.write_text("# Existing Contract\n\nNo pattern block here.\n", encoding="utf-8")
        self.write_json(
            "state/orchestrator.json",
            {
                "schemaVersion": 1,
                "activeProject": "(framework)",
                "phase": "executing",
                "authorizedEpic": "epic-shadow",
                "approvedEpicIds": [],
                "approvedIssueIds": [],
                "authorizationFrozenAt": None,
                "uniqueIssuesDispatched": 0,
                "totalDispatches": 0,
            },
        )
        self.write_issue("epic-shadow", {"id": "epic-shadow", "kind": "epic", "project": "(framework)", "title": "Epic", "state": "Todo", "children": ["shadow-code"]})
        self.write_issue(
            "shadow-code",
            {
                "id": "shadow-code",
                "kind": "task",
                "project": "(framework)",
                "parent": "epic-shadow",
                "title": "Shadow code",
                "state": "Todo",
                "workerMode": "code",
                "proofObjective": "Record a shadow decision only.",
                "allowedPaths": ["scripts/example.py"],
                "dependsOn": ["missing-dependency"],
                "workspace": str(workdir),
            },
        )

        rc, dispatches = self.run_tick()
        self.assertEqual(rc, 0)
        self.assertEqual(dispatches, [])
        issue = json.loads((self.state_dir / "issues" / "shadow-code.json").read_text())
        self.assertEqual(issue["patternDecision"]["selectedPattern"], "code")
        self.assertEqual(issue["patternDecision"]["status"], "shadow_only")
        self.assertNotIn("executionPattern", issue)
        self.assertNotIn("OPENCLAW_PATTERN_DECISION_BEGIN", contract.read_text())

        ledger_path = self.state_dir / "orchestrator-decisions.jsonl"
        first_entries = [json.loads(line) for line in ledger_path.read_text().splitlines() if line.strip()]
        pattern_entries = [entry for entry in first_entries if entry.get("decision") == "shadow_execution_pattern"]
        self.assertEqual(len(pattern_entries), 1)

        rc, dispatches = self.run_tick()
        self.assertEqual(rc, 0)
        second_entries = [json.loads(line) for line in ledger_path.read_text().splitlines() if line.strip()]
        second_pattern_entries = [entry for entry in second_entries if entry.get("decision") == "shadow_execution_pattern"]
        self.assertEqual(len(second_pattern_entries), 1)

    def test_frozen_window_shadow_hold_records_blocked_unapproved_helper_without_dispatch(self) -> None:
        self.write_json(
            "state/orchestrator.json",
            {
                "schemaVersion": 1,
                "activeProject": "optproj",
                "phase": "executing",
                "authorizedEpic": "epic-opt",
                "approvedEpicIds": ["epic-opt"],
                "approvedIssueIds": ["opt-parent"],
                "authorizationFrozenAt": "2026-05-01T00:00:00Z",
                "uniqueIssuesDispatched": 0,
                "totalDispatches": 0,
            },
        )
        self.write_json(
            "state/frozen-authorization.json",
            {
                "project": "optproj",
                "approvedEpicIds": ["epic-opt"],
                "approvedIssueIds": ["opt-parent"],
                "authorizationFrozenAt": "2026-05-01T00:00:00Z",
                "authorizationProject": "optproj",
            },
        )
        self.write_issue("epic-opt", {"id": "epic-opt", "kind": "epic", "project": "optproj", "title": "Epic", "state": "Todo", "children": ["opt-parent"]})
        self.write_issue("opt-parent", optimize_issue(parent="epic-opt"))
        ctx = research_ctx(state={"evaluatorExploitReview": {"status": "pending", "pendingReason": "mid_budget", "sequence": 1}})

        rc, dispatches = self.run_tick(research_context=ctx)
        self.assertEqual(rc, 0)
        self.assertEqual(dispatches, [])
        issue = json.loads((self.state_dir / "issues" / "opt-parent.json").read_text())
        pd = issue["patternDecision"]
        self.assertEqual(pd["selectedPattern"], "evaluator_exploit_review")
        self.assertEqual(pd["status"], "blocked_unapproved_helper")
        self.assertFalse(pd["helperAction"]["dispatchAllowed"])
        self.assertTrue(pd["candidateGenerationSuppressed"])
        self.assertNotIn("executionPattern", issue)


if __name__ == "__main__":
    unittest.main()
