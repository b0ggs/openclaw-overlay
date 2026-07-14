# Prompt-Pack Path Placeholders

Prompt-pack files use the shared `OPENCLAW_*` path mechanism from
`harness/path_config.py` and `scripts/openclaw-paths.sh`.

| Placeholder | Default |
|---|---|
| `${OPENCLAW_HOME}` | `$HOME/.openclaw` |
| `${OPENCLAW_WORKSPACE_ROOT}` | `${OPENCLAW_HOME}/workspace` |
| `${OPENCLAW_MDS_ROOT}` | `${OPENCLAW_HOME}/repos/openclaw-mds` |
| `${OPENCLAW_PROJECTS_ROOT}` | `${OPENCLAW_HOME}/projects` |
| `${OPENCLAW_EXTERNAL_PROJECTS_ROOT}` | `$HOME/projects` |
| `${OPENCLAW_WORKTREES_ROOT}` | `${OPENCLAW_HOME}/worktrees` |
| `${OPENCLAW_STOP_FILE}` | `${OPENCLAW_HOME}/STOP` |
| `${OPENCLAW_RUNS_ROOT}` | `$HOME/tmp/openclaw-runs` |
