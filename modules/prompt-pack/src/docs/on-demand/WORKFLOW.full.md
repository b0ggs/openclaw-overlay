---
# WORKFLOW.md — OpenClaw Orchestration Contract (repo-owned)
# Symphony-style control plane contract + autonomy policy.

schemaVersion: 1

tracker:
  kind: local
  root: state/issues

polling:
  interval_ms: 60000

workspace:
  root: ${OPENCLAW_WORKTREES_ROOT}

states:
  active: [Todo, In Progress, Rework, Human Review, Merging]
  terminal: [Done, Cancelled, Canceled, Duplicate, Blocked]

agents:
  max_concurrent: 4
  max_turns_per_run: 12

execution_pattern_router:
  mode: enforced

merge:
  require_eval_pass: true
  require_review_pass: true
  require_commit_match: true
  skip_for_research_issues: true

publication:
  startup_git_mutation_default: false
  broad_all_files_staging_default: false
  non_trivial_harness_default: review_branch_plus_pr
  direct_push_to_default_ref_default: false
  branch_pushed_is_done: false
  pr_open_is_done: false
  checks_pass_on_branch_is_done: false
  missing_pr_merge_or_publication_permission_status: READY_FOR_HUMAN_REVIEW_OR_MERGE
  missing_pr_merge_or_publication_permission_done_status: NOT_DONE
  agent_github_write_requires_exact_approval: true
  exact_approval_fields: [repo, branch, destinationRef, operation, pathScope, expectedChecks, cleanupDisposition, approvedBy, approvedAt]
  branch_cleanup_human_only_without_exact_branch_approval: true
  default_ref_source: repo_default_or_explicit_issue_contract
  done_requires:
    - approved_changes_landed_on_authorized_destination_default_ref
    - finalizer_evidence_passed
  explicit_exceptions:
    - branch_only
    - draft_only
    - candidate_only
    - local_only
  exception_requires_exact_ref_or_path_and_rationale: true

live_state_policy:
  authoritative_json:
    - state/orchestrator.json
    - state/issues/*.json
  derived_outputs:
    STATE.md: derived-promoted
    state/active-tasks.json: derived-promoted
  derived_output_rules:
    hand_edit: false
    authority: false
    promotion_requires_finalizer_review: true
    stale_or_inconsistent_render_blocks_publication: true
  memory:
    daily_memory_default: local_only_private_runtime
    long_term_memory_default: private_harness_continuity
    raw_memory_publication_default: false
    restore_critical_facts_require_distilled_non_secret_artifact: true
  local_only_whitelist:
    exact_redacted_operation_scoped_and_finalizer_reviewed: true
    authorizes_staging_or_exposure: false
  final_cleanliness:
    repo_changing_publication_default: zero_dirty
    unknown_dirty_blocks_repo_changing_operations: true
    secret_private_blocks_immediately: true

overlay_v1:
  active_protocol:
    module_acceptance: ${OPENCLAW_EXTERNAL_PROJECTS_ROOT}/openclaw-overlay-v1/docs/validation/module-acceptance-protocol.md
    harness_test_taxonomy: ${OPENCLAW_EXTERNAL_PROJECTS_ROOT}/openclaw-overlay-v1/docs/validation/harness-test-taxonomy.md
    module_subagent_packet_template: ${OPENCLAW_EXTERNAL_PROJECTS_ROOT}/openclaw-overlay-v1/docs/validation/module-subagent-packet-template.md
  module_acceptance_rules:
    local_fixtures_are_preflight_only: true
    evals_are_preflight_until_disposable_target_acceptance: true
    per_module_test_manifest_required: true
    per_module_manifest_minimum_fields:
      - module_id
      - owner_issue
      - matrix_row
      - test_taxonomy_categories
      - exact_install_paths
      - expected_write_paths
      - unit_test_commands
      - contract_schema_test_commands
      - local_fixture_commands
      - expected_fail_cases
      - replay_regression_cases
      - eval_suite_id
      - eval_cases
      - eval_metrics
      - eval_pass_thresholds
      - pre_update_target_probe
      - post_update_target_probe
      - cleanup_revert_proof
      - evidence_layout
      - required_audit_roles
    required_target_cycle:
      - revert_disposable_target_to_clean_april_openclaw
      - prove_no_overlay_or_module_installed
      - install_only_module_under_test
      - run_pre_update_module_probe
      - update_same_target_to_pinned_june_openclaw
      - rerun_same_module_probe
      - capture_redacted_write_audit_and_cleanup_evidence
      - revert_target_to_clean_april_before_next_matrix_item
    required_test_classes:
      - unit_tests
      - contract_and_schema_tests
      - local_preflight_positive_and_expected_fail_fixtures
      - property_and_invariant_tests_when_applicable
      - applier_lifecycle_tests
      - replay_and_regression_tests
      - evals_for_behavioral_orchestration_claims
      - security_and_privacy_tests
      - disposable_april_clean_baseline_test
      - pre_update_target_probe
      - post_update_target_probe_after_pinned_june_update
      - cleanup_and_revert_to_clean_april_test
      - evidence_status_gate
    required_audit_roles:
      - module_primary
      - module_test_checker
      - module_ops_update_survival_auditor
      - module_security_privacy_auditor
      - module_mediator_finalizer
    subagent_launch_policy:
      required_reasoning_effort: extra_high_or_tool_equivalent
      obey_current_runtime_fanout_cap: true
      roles_may_run_in_sequential_waves: true
      primary_must_not_be_final_mediator: true
      dispatch_prompt_must_include:
        - module_acceptance_protocol
        - harness_test_taxonomy
        - module_subagent_packet_template
        - module_test_manifest
        - exact_allowed_paths
        - role_specific_outputs
        - stop_conditions
    v1_green_requires_real_module_acceptance: true

autonomy:
  planned_means_execute: true
  default_max_required_children: 20
  max_unique_issues_per_window: 30
  max_total_dispatches_per_window: 100
  allow_project_scope_blanket_go: true
  epic_completion_is_not_stop_boundary: true
  final_completion_required_for_ready: true

  progress_updates:
    ask_human_on_milestones: false
    terse_milestones: true
    long_running_interval_minutes: 10
    preferred_template: "Deliverable {done}/{total} finished — continuing work"
    runtime_pulse_script: scripts/progress-pulse.py
    runtime_pulse_via_main_session_cron: true

  deliverable_bound_execution: true
  continue_means_within_existing_list_only: true
  elliptical_references_require_authorized_list_resolution: true
  compaction_summaries_are_not_authorization: true
  context_recovery_block_status: CONTEXT_RECOVERY_BLOCKED
  prep_artifacts_count_as_new_deliverables: true
  stop_when_list_exhausted: true

  research_stop_conditions:
    - budget_exhausted
    - deadline_passed
    - truth_unavailable
    - flatline_after_resets
    - target_score_reached
  research_stops_override_blanket_go: true

  blanket_go_phrases:
    - go
    - finish what you can
    - keep going
    - continue until blocked
    - do the whole project
    - complete the project

  default_stop_only_if:
    - missing_required_secret
    - missing_required_permission
    - destructive_action_outside_allowlist
    - resource_limit_exceeded
    - scope_expansion_required

  default_ask_before:
    - external_message
    - real_money
    - private_key_access
    - irreversible_delete_outside_scratch

quarantine:
  enabled: true
  runtime_enforced: true
  required_fields:
    - workspaceIsolation
    - locks
    - allowedPaths
  multi_worker:
    enabled: false
    allow_only_if_quarantined: true
    suggest_enable_when:
      - issue.quarantine == true
      - issue.workspaceIsolation != null
      - issue.locks length > 0
  evaluator:
    autoRevert_default: false
    suggest_enable_when:
      - issue.objective.optimization == true

pipeline:
  auto_restart_default: false

research:
  phases:
    allowed: [setup, baseline, search, calibrate, submission_freeze, blocked_truth_unavailable, blocked_flatline, done]
    initial: setup
  scope_guardrail:
    optimize_mode_is_for_narrow_artifacts_only: true
    framework_or_runtime_changes_use_code_or_hybrid: true
  archive:
    raw_traces_required_for_serious_evals: true
    machine_readable_metrics_required: true
    results_ledger_is_index_not_source_of_truth: true
  truth_manager:
    no_truth_no_search: true
    truth_failure_threshold: 3
    reserve:
      calibration_evaluations: 12
      final_validation_evaluations: 16
  canonical_sync:
    enforce: true
    mismatch_action: block
  budget:
    enforce_hard_cap: true
    consolidation_threshold_pct: 20
  deadline:
    auto_freeze: true
    auto_stop_candidate_generation_at_deadline: true
    post_deadline_default: finalization_only
  plateau:
    incremental_threshold: 3
    total_threshold: 12
    flatline_action: block
  search_strategy:
    optimize_for_information_gain: true
    prefer_breakthroughs_to_micro_tuning: true
    reset_after_plateau: true
  parallel:
    default_workers: 2
    max_workers_per_project: 4
    auto_fanout: true
    require_lane_isolation: true
    use_sibling_issues_not_same_issue_multiworker: true
    serialize_promotions: true
  fanout_heuristics:
    max_eval_latency_seconds_for_fanout: 120
    min_budget_remaining_pct_for_fanout: 30
  mixed_execution:
    enabled: true
    project_mode_is_not_global_lock: true
  distillation:
    living_doc: DISTILLATION.md
    update_on_meaningful_events: true
    reader_has_not_read_results_ledger: true
    keep_shorter_than_raw_traces: true
    require_dead_end_capture: true
    require_transferable_insight_memo: true
  outer_loop:
    critic_reads_raw_archive: true
    critic_on_flatline: true
    critic_on_proxy_truth_mismatch: true
    entropy_on_flatline_playbook:
      ordered: true
      max_iterations_per_step: 1
      sequence:
        - literature_refresh
        - alt_framing_critic
        - reset_blind_lane
        - champion_local_exploit
    evaluator_exploit_review:
      required_before_search_after_mid_budget_or_score_jump: true
      before_final_validation: true
      mid_budget_pct: 50
      score_jump_pct: 10
      max_iterations: 1
  finalization:
    done_requires_verified_export: true
    auto_create_final_validation_issue: true
    auto_create_submission_bundle_issue: true
    keep_submission_freeze_until_export_ready: true
  progress:
    meaningful_events:
      - champion_replacement
      - dead_end_confirmed
      - truth_restored
      - calibration_completed
      - submission_bundle_verified
      - phase_transition
    suppress_activity_only_updates: true
  watchdog:
    checks:
      - results_tsv_recently_appended
      - budget_remaining_above_zero
      - deadline_not_passed
      - truth_available
      - not_flatlined
    forbidden_watchdog_phrases:
      - "do not let workers stop"
      - "keep workers saturated"
      - "maintain worker pools"
      - "do not let challenge workers stop"

code_scoring:
  enabled: true
  require_evidence_for_handoff: true
  require_threshold_or_diminishing_returns_note: true
  default_ledger_dir: state/evals

self_improvement:
  mode: safe_default
  runtime_status: enabled
  budgets:
    max_active_candidates: 1
    max_parallel_replays: 2
    max_replay_cases_per_candidate: 8
    max_wall_clock_seconds_per_candidate: 7200
    require_clean_workspace: true
  retrospective:
    enabled: true
    worker_mode: bridge_research
    detected_reasons:
      - completion
      - blocked
      - flatline
      - truth_outage
      - repeated_rework
      - regression
    reason_enum:
      - completion
      - blocked
      - review_disagreement
      - repeated_rework
      - flatline
      - truth_outage
      - regression
    dedupe_window_hours: 24
    max_followup_issues_per_source_issue: 1
    control_plane_maintenance_not_scope_expansion: true
    authorization:
      during_frozen_window: queue_only_unless_predeclared
      immediate_dispatch_allowed_if_predeclared_child_issue: true
      otherwise_dispatch_after_window: true
    suppress_when:
      - trivial_no_signal
      - duplicate_failure_with_recent_postmortem
    outputs:
      - postmortem
      - improvement_backlog_update
      - research_program_update
      - dead_end_capture
      - distillation
      - harness_candidate_proposal
  proposal_classes:
    canonical:
      - doc_only_project_knowledge
      - project_process_change
      - harness_candidate
    handling:
      auto_apply:
        - doc_only_project_knowledge
      sandbox_first:
        - project_process_change
        - harness_candidate
      require_cross_project_evidence_for_default_promotion:
        - harness_candidate
    auto_apply_boundary:
      local_only: true
      no_git_publication_authority: true
      no_harness_policy_runtime_or_default_prompt_changes: true
      behavior_changes_require_sandbox_or_replay: true
      default_promotion_requires_human_approval: true
  harness_candidates:
    require_machine_readable_implementation_ref: true
    replay_before_promotion: true
    candidate_status_lifecycle:
      - proposed
      - shadow_test
      - piloting
      - promoted
      - rejected
      - rolled_back
    rollout_states:
      - shadow_only
      - project_local_opt_in
      - global_opt_in
      - default
      - shadow_test
    trial_states:
      - replay_queued
      - replay_running
      - replay_completed
      - replay_failed
    promotion:
      project_local_requires_replay_evidence: true
      default_requires_cross_project_evidence: true
      default_requires_project_type_count: 2
      default_requires_human_approval: true
  safety:
    no_live_rewrite_of_running_workers: true
    no_recursive_retrospectives: true
    no_auto_expansion_of_user_deliverables: true
    no_unbounded_cron_growth: true

hooks:
  after_create: null
  after_run: null
---

You are operating under this workflow contract.

Rules:
1. **Foundation before scaffolding or resume.** No new project may be scaffolded, and no resumed project may re-enter substantive planning or execution, unless a valid initiation root exists and `handoff-guard` passes. If not, stay in PLANNING or BLOCKED and route to project initiation.
2. **Foundation artifacts are upstream authority.** Harness-side spikes, runbooks, compiler experiments, and evaluation packets may not define project truth unless the user explicitly promotes them.
3. Do not ask the human for the next planned step if required deliverables/issues remain open and work is within scope and policy.
4. Continue until the current issue reaches its handoff state or a true blocker occurs.
5. If one deliverable is completed and required deliverables remain open, immediately continue to the next eligible deliverable/issue.
6. If the current epic reaches handoff/completion and project-scope blanket-go still covers additional already-planned in-scope work, immediately continue to the next highest-priority eligible epic/wave; epic completion alone is not a stop boundary.
7. **Deliverable-bound execution:** when the user provides an explicit issue graph, deliverable list, execution package, or ordered scope, execute only those listed items.
8. **Continue / keep going / resume / all non-X steps** mean continue within the existing authorized list only; they do not authorize creating new deliverables, issue IDs, prompts, packets, memos, bundles, onboarding docs, checklists, manifests, helper scripts, or a new wave.
9. If all listed items are complete and further progress would require new deliverables or a new wave, stop and report **`LIST_EXHAUSTED`** / scope expansion required rather than inventing more work.
10. **Blocked means blocked:** if progress now depends on missing infrastructure, credentials, access, or a different machine, report the blocker and do not invent supporting deliverables unless explicitly requested.
11. **Prep artifacts count as deliverables.** Handoff docs, onboarding packets, rerun bundles, review packets, checklists, manifests, helper scripts, evidence hunts, and similar prep work are new deliverables unless already on the approved list.
12. Only interrupt the human for a true blocker, an explicit stop/pause instruction, or final completion of the authorized scope.
13. Terse progress updates are allowed and preferred after substantive deliverables/issues and during long-running work, but they must not be phrased as approval requests.
14. Before an execution wave begins, freeze authorization explicitly with `scripts/freeze-authorization.py <approved-epic-id> [...]`. The frozen set is the explicit root epic IDs plus the IDs named in each root epic's `children` array. For approved non-lane optimize parent issues, freeze also predeclares deterministic internal helper IDs for isolated fanout lanes and finalization helpers so those execution-pattern helpers remain dispatch-legal inside the frozen window.
15. During a frozen execution window, dispatch is fail-closed: only `approvedEpicIds` may become `authorizedEpic`, only `approvedIssueIds` may be dispatched, and new Todo/Rework issues outside that frozen set must be blocked/quarantined rather than dispatched.
16. Frozen authorization metadata is immutable during execution. Do not widen authorization metadata while `authorizationFrozenAt` is set.
17. Frozen execution is capped by both `autonomy.max_unique_issues_per_window` and `autonomy.max_total_dispatches_per_window`.
18. Successful dispatches append machine-readable audit events to `state/runs/dispatch-audit.jsonl`; frozen-window quarantines append audit events to `state/runs/quarantine-audit.jsonl`.
19. Operator kill switch: if `${OPENCLAW_STOP_FILE}` exists, tick dispatch must stop immediately.
20. Do not expand scope silently; create a follow-up note or issue for meaningful out-of-scope work during planning, not mid-flight execution.
21. Never relax ask-before boundaries.
22. **Optimization uses `workerMode: optimize`.** Keep analyst-style `research` for evidence and reporting work.
23. **No truth, no search.** When the evaluator is broken or truth budget is unavailable, candidate generation must stop.
24. **Canonical sync is mandatory.** The champion manifest, canonical artifact(s), and results ledger must agree.
25. **Deadline is a kill switch for candidate-generation optimize work.** At deadline, candidate-generation optimize dispatch stops and active candidate-generation workers are terminated, but bounded `final_validation` and `submission_bundle` work may continue until verify/export completes.
26. **Research budgets are hard caps.** Budget exhaustion ends search; it does not trigger a new wave of candidate generation.
27. **Optimize mode is narrow.** Use `workerMode: optimize` only for score-driven project artifact search. If the work changes workspace harness, runtime, or framework code, use `code` or `hybrid` so the auditor chain still applies.
28. **Raw traces beat summaries.** For research work, ledgers are indexes; diagnosis should use raw traces, code, validator output, and metrics artifacts.
29. **Distill, don't just optimize.** After meaningful wins, dead ends, or phase changes, write the transferable lesson into project research docs.
30. **Protect the thinker bottleneck.** Improve evaluation plumbing and artifact organization so model reasoning, not serial overhead, is the limiting factor.
31. **Run the entropy playbook when the hill gets flat.** Repeated tiny non-improvements trigger this ordered, bounded sequence before local grinding resumes: literature refresh -> alt-framing critic -> reset-blind lane -> champion-local exploit/tweak.
32. **Check for evaluator gaming before continuing search.** At mid-budget or after a significant score jump, queue a bounded evaluator-exploit critic memo and pause candidate generation until it asks whether the champion reflects real insight or evaluator weakness.
33. **Optimize fanout needs isolated lanes.** Do not use same-issue multi-worker for optimize-mode search. Parallel optimize work must use sibling lane issues with separate workspaces/worktrees and serialized promotion.
34. Low-risk bounded work may be single-worker.
35. Substantive work requires a named primary worker and a named independent checker. This applies to planning, research, policy, architecture, evaluation, audit, and code, not just implementation diffs.
36. Authority-setting, high-risk, security/safety-sensitive, or materially disputed work requires mediator before acceptance or promotion.
37. If review-tier classification is ambiguous, escalate upward.
38. The orchestrator does not count as an independent pass.
39. **Automatic retrospectives are allowed.** They are control-plane maintenance, not silent expansion of the user's deliverable scope.
40. **Retrospectives must be bounded and deduped.** At most one bounded follow-up per source issue transition, and never a recursive retrospective chain.
41. **Low-risk knowledge capture may auto-apply locally only.** Postmortems, dead ends, distillations, and project runbook clarifications may be updated automatically when grounded in evidence, but `auto_apply` does not authorize Git publication.
42. **Auto-apply cannot change harness authority.** Prompt defaults, role contracts, routing, runtime behavior, workflow policy, and other behavior-changing harness ideas must be sandboxed, replayed, or otherwise evidenced before promotion, and default promotion requires human approval.
43. **Do not live-rewrite the baseline harness.** Automatic self-improvement should propose and test first, then promote conservatively; it may not mutate live harness policy/runtime/default prompts under the `auto_apply` lane.
44. **Frozen-window authorization still wins.** During a frozen execution window, automatic retrospectives may dispatch immediately only if they were predeclared inside the approved set; otherwise they must be queued for later maintenance.
45. **Do not let retrospectives steal the lane.** Automatic retrospectives may dispatch only when no authorized user-scope work remains open.
46. **Quarantine claims must be real.** Phase 1 allows single-worker quarantined dispatch only when the runtime wrapper can enforce read-only harness/project/workspace roots plus declared `allowedPaths`. `allowedPaths` must not reopen workspace control-plane surfaces, live root control files, or the workspace root itself, named project roots must already exist, and symlink routes that create non-allowed writable aliases or escape protected roots must fail closed. Multi-worker remains disabled, and quarantine still does not claim full machine or network sandboxing.
47. **Harness-candidate trials are maintenance, not a new worker family.** Express them with `maintenance.kind` and/or `executionPattern`, not a new top-level `workerMode` taxonomy.
48. **Framework edits are not optimize-mode artifact search.** Workspace / harness / runtime changes must stay in `code` or bounded maintenance flows with replay evidence, not ordinary `workerMode: optimize` candidate generation.
49. **Behavior-changing harness candidates need a real implementation reference.** A replayable candidate must include a machine-readable materialization reference, replay plan, touched paths, and rollback path before execution.
50. **Default-harness promotion is conservative.** Replay evidence is required for any behavior-changing candidate, default promotion needs evidence across at least two project types, and this wave still requires human approval for default promotion.
51. **Replay isolation is fail-closed.** Harness candidate replays must run in isolated self-improvement roots, record comparable inputs for baseline and candidate, and must not mutate live workspace/project control-plane files.
52. **Completion finalizer guard is a chokepoint.** Required framework/control-plane or material-path issues may not transition to `Human Review`, `Done`, or READY/scope-complete reconciliation unless `status.finalizer` contains structured passed evidence (`subjectCommit`/`subjectCommitOid`, path metadata, matching destination when known, and no errors) or a strict `status.finalizer.localOnly` exception with exact paths plus rationale. Human Review alone is not finalization evidence; epic auto-advance and READY reconciliation must fail closed when required child evidence is absent. A `Done` transition additionally requires branch/PR/default-ref lifecycle proof: branch-only candidates, open/unmerged PRs, missing publication permission, or missing destination/default-ref proof are `READY_FOR_HUMAN_REVIEW_OR_MERGE` with `doneStatus=NOT_DONE`, not success.
53. **Startup is not Git mutation authority.** Startup reads may inspect repository status, but they do not authorize pull/fetch/rebase/checkout or any other checkout mutation.
54. **Stage exactly scoped paths.** Publication lanes must stage intentional paths from the issue contract/finalizer evidence; broad all-files staging is not a default path.
55. **Review before default-ref landing.** Non-trivial harness/control-plane/policy/runtime/default-prompt work defaults to a reviewable branch plus GitHub PR or explicitly equivalent review object before landing on the authorized destination/default ref.
56. **Branch or PR is not done.** A pushed branch, open PR, or draft candidate is a handoff/review state, not `Done`, unless the approved issue contract explicitly says branch-only, draft-only, candidate-only, or local-only completion is the deliverable.
57. **Done requires destination evidence.** Git-backed harness work reaches `Done` only when finalizer evidence proves the approved subject landed on the authorized destination/default ref, or records an explicit exact-path/ref exception and rationale.
58. **Direct default-ref push is exceptional.** Direct push to the default ref is not the default publication path for non-trivial harness work and requires explicit low-risk scope plus secret preflight and exact path staging.
59. **GitHub write approval is exact and operation-specific.** Token presence, prior authentication, broad blanket-go, or earlier branch review is not approval to create/update PRs, push branches, merge PRs, push default refs, delete branches/tags, mutate settings, or mutate credential lanes. If exact approval is absent, prepare evidence for the human and leave the issue `NOT_DONE`.
