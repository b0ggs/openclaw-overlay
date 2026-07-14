# SKILLS ROUTING

When spawning agents, include relevant skills in their prompt.

## Skill Locations

| Source | Path |
|--------|------|
| EthSkills | `${OPENCLAW_HOME}/skills/ethskills/` |
| Base | `${OPENCLAW_HOME}/skills/base-skills/skills/` |
| Bankr | `${OPENCLAW_HOME}/skills/bankr-skills/` |
| Trail of Bits | `${OPENCLAW_HOME}/skills/tob-skills/plugins/` |
| ToB Curated | `${OPENCLAW_HOME}/skills/tob-curated/` |

---

## Project-initiation routing

Route new or resumed project requests to `skills/project-initiation/` first.

Examples:
- "start a project"
- "new project"
- "restart project"
- "resume project"
- "define success first"
- "do a lit review first"

`init-project.sh` is not the first step. It is only allowed after the project-initiation handoff passes.

## Worker-mode routing

Worker modes route to role documents. They are not the same thing as launchable
named-agent profiles in `openclaw.json`.

- `workerMode: code` -> `CODER.md`
- `workerMode: research` -> `ANALYST.md`
- `workerMode: optimize` -> `RESEARCHER.md`
- `workerMode: pipeline` -> `PIPELINER.md`
- `workerMode: bridge_research` -> `ANALYST.md`

Current launchable named profiles are `codex`, `auditor-alpha`,
`auditor-alpha-prime`, and `auditor-beta`, all on `openai-codex/gpt-5.5`.
Use `codex` only as a generic backend/fallback profile when a script requires
it; the task prompt and role doc still define the role. Do not route work to
deleted `arena-*` named agents.

Review-dispatch reviewers are prompt roles:
- `REVIEWER_DATA.md` for evidence, manifests, and report correctness
- `REVIEWER_OPS.md` for pipeline safety, restartability, and runbooks

They may run through the `codex` backend in current scripts, but they are not
security auditors and not standalone named-agent profiles.

Execution patterns may further refine the route inside one project:
- `code`
- `scored_code`
- `optimize_single`
- `optimize_fanout`
- `critic`
- `finalization`
- `harness_candidate_trial`

Use `research` for evidence packs, validation, reports, model comparisons, review packets, and environment diagnosis.
Use `bridge_research` for outer-loop critique: archive inspection, bottleneck diagnosis, proxy/truth mismatch analysis, search-program revision, and bounded retrospectives / postmortems.

Use `optimize` for score-driven candidate -> evaluate -> compare -> promote work on narrow project artifacts.
If the task changes workspace/runtime/framework code rather than a project artifact, use `code` or `hybrid`, not `optimize`.
Use `scored_code` when the issue is normal engineering work but also carries a measurable score such as gas, latency, throughput, memory, coverage, or bundle size.
Use `bridge_research` to propose a `harness_candidate`, grounded in evidence, with a replay plan and rollback path.
Use `code` for the actual framework/harness implementation slice that produced the candidate artifact/reference.
Use the control-plane replay/comparison scripts for `harness_candidate_trial`; do not treat replay execution as free-form optimize work.
For `optimize` and `bridge_research`, follow `harness/playbooks/research.md` in addition to the worker prompt.

---

## Optimization Arena 2026 Hackathon

For the Optimization Arena hackathon, treat the Arena pack as the governing workflow rather than the default project loop.

### Installed Arena skills

| Skill | Path |
|-------|------|
| challenge-intake | `skills/challenge-intake/` |
| stochastic-optimize | `skills/stochastic-optimize/` |
| submission-readiness | `skills/submission-readiness/` |
| negotiation-prompt | `skills/negotiation-prompt/` |
| prediction-market-mm | `skills/prediction-market-mm/` |
| attention-kernel | `skills/attention-kernel/` |
| modal-h100-runner | `skills/modal-h100-runner/` |

### Arena routing rule

- All three tracks start with `challenge-intake`
- Use `stochastic-optimize` as the default noisy-score search loop
- Use `submission-readiness` before any submit / export / final handoff decision
- Negotiation track adds `negotiation-prompt`
- Prediction-market track adds `prediction-market-mm`
- Attention-kernel track adds `attention-kernel` and `modal-h100-runner`

Pack source/checklist preserved at:
`projects/optimization-arena-2026/_skill-pack-source/`

## Agent -> Skill Mapping

### ORCHESTRATOR (Planning)

| Skill | Path |
|-------|------|
| why.md | ethskills/why.md |
| ship.md | ethskills/ship.md |
| concepts.md | ethskills/concepts.md |

### CODER (Implementation)

**EthSkills:**
- ship.md, security.md, testing.md, tools.md, standards.md
- gas.md, addresses.md, orchestration.md
- frontend-ux.md, frontend-playbook.md
- building-blocks.md, wallets.md, l2s.md, indexing.md

**Base:**
- base-skills/skills/deploying-contracts-on-base/
- base-skills/skills/connecting-to-base-network/
- base-skills/skills/building-with-base-account/
- base-skills/skills/converting-minikit-to-farcaster/

**Bankr (selected):**
- bankr-skills/onchainkit/ (React components for Base)
- bankr-skills/siwa/ (Sign-In With Agent, ERC-8004 auth)
- bankr-skills/yoink/ (game pattern reference)

### RESEARCHER (Optimization)

Use the optimization path for score-driven work where the inner loop is:
candidate -> evaluate -> compare -> promote.

Research-mode expectations:
- read `RESEARCH_PROGRAM.md`, recent ledgers, dead ends, and raw archive traces when diagnosing
- optimize for information gain, not just activity
- preserve queryable traces so bridge-research critics can inspect them later

Primary Arena skills:
- `skills/challenge-intake/`
- `skills/stochastic-optimize/`
- `skills/submission-readiness/`

Domain-specific additions when relevant:
- `skills/negotiation-prompt/`
- `skills/prediction-market-mm/`
- `skills/attention-kernel/`
- `skills/modal-h100-runner/`

### ANALYST (Evidence / validation)

Use the analyst path for:
- evidence packs
- model comparisons
- environment diagnosis
- review packets
- measurements that do not directly own the champion artifact

For `bridge_research`, the analyst is the outer-loop critic:
- inspect `RESEARCH_PROGRAM.md`, `research/results.tsv`, `research/dead_ends.md`, and `research/archive/`
- diagnose bottlenecks, brittle wins, confounds, and proxy/truth mismatch
- write postmortems and improvement-backlog entries when the system learned something reusable
- improve the search program or distillation, not the champion artifact itself

### PIPELINER (Long-running jobs)

Use `PIPELINER.md` for resumable/idempotent ingest, backfill, build, or
pipeline work. Focus skills on observability, checkpointing, manifests, and
restart safety.

### REVIEWER_DATA / REVIEWER_OPS (Review dispatch)

Use `REVIEWER_DATA.md` for evidence/report validation and `REVIEWER_OPS.md`
for long-running job safety reviews. Current dispatch scripts can run these
prompt roles via the `codex` backend profile.

### CODEX backend profile

`CODEX.md` documents the `codex` named runtime profile. It supplies a generic
GPT-5.5 backend only; do not treat it as a worker-mode role or skill route.

### AUDITOR_ALPHA & AUDITOR_ALPHA_PRIME (Security)

**EthSkills:**
- security.md, standards.md, addresses.md, building-blocks.md

**Trail of Bits:**
- tob-skills/plugins/building-secure-contracts/ (vuln scanners, audit prep)
- tob-skills/plugins/audit-context-building/ (line-by-line analysis)
- tob-skills/plugins/differential-review/ (git diff security review)
- tob-skills/plugins/entry-point-analyzer/ (state-changing entry points)

**ToB Curated:**
- tob-curated/scv-scan/ (36 Solidity vulnerability classes)

### AUDITOR_BETA (Edge Cases & QA)

**EthSkills:**
- concepts.md, testing.md, qa.md

**Trail of Bits:**
- tob-skills/plugins/supply-chain-risk-auditor/ (dependency risk)
- tob-skills/plugins/building-secure-contracts/ (code maturity assessor)

**ToB Curated:**
- tob-curated/scv-scan/

---

## Quick Reference

| Need | Use |
|------|-----|
| Challenge intake | challenge-intake |
| Noisy-score search | stochastic-optimize |
| Final optimization handoff | submission-readiness |
| Deploy to Base | base-skills/deploying-contracts-on-base |
| Agent-only auth | bankr-skills/siwa + ethskills/standards.md (ERC-8004) |
| React components | bankr-skills/onchainkit |
| Security scan | tob-skills/building-secure-contracts, tob-curated/scv-scan |
| Pre-audit prep | tob-skills/audit-context-building |
| Dependency risk | tob-skills/supply-chain-risk-auditor |
