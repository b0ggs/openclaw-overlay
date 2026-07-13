---
schemaVersion: 1
tracker: {kind: local, root: state/issues}
polling: {interval_ms: 60000}
workspace: {root: ~/.openclaw/worktrees}
states: {active: [Todo, In Progress, Rework, Human Review, Merging], terminal: [Done, Cancelled, Canceled, Duplicate, Blocked]}
agents: {max_concurrent: 4, max_turns_per_run: 12}
execution_pattern_router: {mode: enforced}
merge: {require_eval_pass: true, require_review_pass: true, require_commit_match: true, skip_for_research_issues: true}
publication:
  startup_git_mutation_default: false
  broad_all_files_staging_default: false
  non_trivial_harness_default: review_branch_plus_pr
  direct_push_to_default_ref_default: false
  branch_pushed_is_done: false
  pr_open_is_done: false
  missing_pr_merge_or_publication_permission_done_status: NOT_DONE
  agent_github_write_requires_exact_approval: true
  done_requires: [approved_changes_landed_on_authorized_destination_default_ref, finalizer_evidence_passed]
  explicit_exceptions: [branch_only, draft_only, candidate_only, local_only]
live_state_policy:
  authoritative_json: [state/orchestrator.json, state/issues/*.json]
  derived_outputs: {STATE.md: derived-promoted, state/active-tasks.json: derived-promoted}
  memory:
    long_term_memory_default: private_harness_continuity
    raw_memory_publication_default: false
  local_only_whitelist:
    authorizes_staging_or_exposure: false
  final_cleanliness:
    repo_changing_publication_default: zero_dirty
    unknown_dirty_blocks_repo_changing_operations: true
    secret_private_blocks_immediately: true
autonomy:
  default_max_required_children: 20
  max_unique_issues_per_window: 30
  max_total_dispatches_per_window: 100
  allow_project_scope_blanket_go: true
  epic_completion_is_not_stop_boundary: true
  deliverable_bound_execution: true
  continue_means_within_existing_list_only: true
  context_recovery_block_status: CONTEXT_RECOVERY_BLOCKED
  prep_artifacts_count_as_new_deliverables: true
  stop_when_list_exhausted: true
  progress_updates: {ask_human_on_milestones: false, terse_milestones: true}
  blanket_go_phrases: [go, keep going, resume, continue until blocked]
quarantine:
  enabled: true
  runtime_enforced: true
  required_fields: [workspaceIsolation, locks, allowedPaths]
  multi_worker: {enabled: false, allow_only_if_quarantined: true}
research:
  phases: {allowed: [setup, baseline, search, calibrate, submission_freeze, blocked_truth_unavailable, blocked_flatline, done], initial: setup}
  truth_manager: {no_truth_no_search: true, truth_failure_threshold: 3}
  parallel: {default_workers: 2, max_workers_per_project: 4, require_lane_isolation: true}
code_scoring: {enabled: true, require_evidence_for_handoff: true}
self_improvement:
  mode: safe_default
  runtime_status: enabled
  proposal_classes:
    handling:
      auto_apply: [doc_only_project_knowledge]
    auto_apply_boundary:
      local_only: true
      no_git_publication_authority: true
      no_harness_policy_runtime_or_default_prompt_changes: true
      behavior_changes_require_sandbox_or_replay: true
      default_promotion_requires_human_approval: true
hooks: {after_create: null, after_run: null}
---

No new project may be scaffolded without project initiation. Foundation artifacts are upstream authority. Rules 1-20: continue through approved work; deliverable-bound execution; `continue`/`keep going`/`resume` stay authorized; report `LIST_EXHAUSTED`; blocked means blocked; prep artifacts count as deliverables; freeze before waves; `${OPENCLAW_STOP_FILE}` halts dispatch.
Rule 52 finalizer guard covers `Human Review`, `Done`, and READY. Startup is not Git mutation authority. Stage exactly scoped paths. Review before default-ref landing with a GitHub PR or explicitly equivalent review object. Branch or PR is not done; a pushed branch, open PR, or draft candidate is not `Done` unless branch-only, draft-only, candidate-only, or local-only completion is explicit. Done requires destination evidence on the authorized destination/default ref. Direct default-ref push is exceptional. GitHub write approval is exact.
`auto_apply` does not authorize Git publication. Auto-apply cannot change harness authority and may not mutate live harness policy/runtime/default prompts.
