#!/bin/bash

set -euo pipefail

repo_path="${1:-$PWD}"
if [[ $# -gt 0 && "${1:-}" != --* ]]; then
  shift
else
  repo_path="$PWD"
fi

declared_args=("$@")

if ! git -C "$repo_path" rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "❌ Not a git repository: $repo_path" >&2
  exit 1
fi

repo_root="$(git -C "$repo_path" rev-parse --show-toplevel)"
cd "$repo_root"

if [ ! -f .gitignore ]; then
  echo "❌ Refusing to continue: missing $repo_root/.gitignore" >&2
  echo "   Add a root .gitignore before committing or pushing anything." >&2
  exit 1
fi

python3 - "${declared_args[@]}" <<'PY'
import json
import re
import subprocess
import sys
from pathlib import Path

repo_root = Path.cwd().resolve()

ALLOWLIST = [
    re.compile(r'(^|/).*([._-]example)(\.[^/]+)?$', re.I),
    re.compile(r'(^|/).*([._-]sample)(\.[^/]+)?$', re.I),
    re.compile(r'(^|/).*([._-]template)(\.[^/]+)?$', re.I),
    # Issue tracker records may legitimately include words like "session" in
    # their IDs. Path allowlisting only suppresses broad local-only path-name
    # heuristics; content scanning still runs on these files.
    re.compile(r'^state/issues/[^/]+\.json$', re.I),
    re.compile(r'^state/archived-issues/[0-9]{4}/[0-9]{2}/[^/]+\.json$', re.I),
    re.compile(r'^state/execution-obligations/[^/]+\.json$', re.I),
]

PATH_RULES = [
    ("local virtualenv", re.compile(r'(^|/)(\.venv|\.venvs|venv)($|/)')),
    ("env file", re.compile(r'(^|/)\.env($|\.)')),
    ("direnv file", re.compile(r'(^|/)\.envrc$')),
    ("secrets directory", re.compile(r'(^|/)(\.secrets|secrets|credentials)($|/)')),
    ("auth profile", re.compile(r'(^|/)(auth|auth[-_]?profiles?[^/]*|auth\.json|auth\.ya?ml)($|/)', re.I)),
    ("raw session transcript", re.compile(r'(^|/)(sessions?|session-transcripts?|session_transcripts?|transcripts?)($|/)|(^|/)[^/]*session[^/]*\.(jsonl?|log|txt)$', re.I)),
    ("raw diagnostics", re.compile(r'(^|/)(raw[-_]?diagnostics?|diagnostics/raw)($|/)|(^|/)[^/]*raw[-_]?diagnostics?[^/]*\.(jsonl?|log|txt)$', re.I)),
    ("volatile dreams", re.compile(r'(^|/)memory/(\.dreams|dreaming)($|/)')),
    ("memory dump", re.compile(r'(^|/)memory/(dumps?|[^/]*dump[^/]*)($|/)|(^|/)[^/]*memory[-_]?dump[^/]*($|\.)', re.I)),
    ("stale live config snapshot", re.compile(r'(^|/)openclaw\.json(?:\.[^/]+)*\.(?:bak|backup|old|orig|save|tmp|snapshot|snap)(?:\.[^/]+)*$|(^|/)openclaw\.[^/]*\.(?:snapshot|snap|backup|bak|old|orig|save|tmp)\.json$|(^|/)[^/]*(?:openclaw[^/]*config|live-config)[^/]*snapshot[^/]*\.(?:jsonl?|ya?ml|txt)$', re.I)),
    ("private credential file", re.compile(r'\.(pem|key|p12|pfx|crt|cer|der|jks|kdbx|pkcs12|agekey|secret|secrets|token|credentials|local|vault)$', re.I)),
    ("credential json", re.compile(r'(^|/)\.credentials(\.[^/]+)?\.json$', re.I)),
]

CONTENT_RULES = [
    ("GitHub fine-grained PAT", re.compile(r'github_pat_[A-Za-z0-9_]{20,}')),
    ("GitHub token", re.compile(r'gh[pousr]_[A-Za-z0-9_]{20,}')),
    ("OpenAI-style key", re.compile(r'sk-[A-Za-z0-9_-]{20,}')),
    ("AWS access key", re.compile(r'AKIA[0-9A-Z]{16}')),
    ("private key block", re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
    ("Slack token", re.compile(r'xox[baprs]-[A-Za-z0-9-]{10,}')),
    ("Bearer token", re.compile(r'authorization\s*:\s*bearer\s+\S{12,}', re.I)),
    ("credential-in-URL", re.compile(r'https://[^/@\s:]+:[^@\s]+@[^\s/]+', re.I)),
]

OPENCLAW_CONFIG_KEYS = {
    "agents",
    "auth",
    "commands",
    "gateway",
    "hooks",
    "messages",
    "plugins",
    "session",
    "skills",
    "tools",
    "wizard",
}

MAX_CONTENT_SCAN_BYTES = 2_000_000


def is_allowlisted(path: str) -> bool:
    return any(rule.search(path) for rule in ALLOWLIST)


def get_lines(*args: str) -> list[str]:
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    data = proc.stdout.decode('utf-8', errors='ignore')
    return [line for line in data.split('\0') if line] if '\0' in data else [line for line in data.splitlines() if line]


def normalize_repo_path(raw: str) -> str:
    raw_path = Path(raw).expanduser()
    candidate = raw_path.resolve() if raw_path.is_absolute() else (repo_root / raw_path).resolve()
    try:
        rel = candidate.relative_to(repo_root)
    except ValueError:
        raise ValueError(f"declared path escapes repository: {raw}")
    rel_posix = rel.as_posix()
    return "." if rel_posix == "." else rel_posix


def parse_declared_paths(argv: list[str]) -> list[str]:
    if not argv:
        return []
    if argv[0] == "--paths":
        raw_paths = argv[1:]
    elif argv[0] == "--":
        raw_paths = argv[1:]
    else:
        raise ValueError("unknown git-preflight option; use --paths <repo-relative paths>")
    normalized: list[str] = []
    for raw in raw_paths:
        if raw == "":
            continue
        normalized.append(normalize_repo_path(raw))
    return sorted(set(normalized))


def expand_declared_paths(paths: list[str]) -> list[str]:
    if not paths:
        return []
    expanded: set[str] = set()
    for rel in paths:
        if rel == ".":
            expanded.update(get_lines('git', 'ls-files', '-z'))
            expanded.update(get_lines('git', 'ls-files', '--others', '--exclude-standard', '-z'))
            continue
        expanded.add(rel)
        target = repo_root / rel
        if target.is_dir():
            expanded.update(get_lines('git', 'ls-files', '-z', '--', rel))
            expanded.update(get_lines('git', 'ls-files', '--others', '--exclude-standard', '-z', '--', rel))
    return sorted(expanded)


def path_rule_hits(paths: list[str]) -> list[str]:
    hits: list[str] = []
    for path in paths:
        if is_allowlisted(path):
            continue
        for label, rule in PATH_RULES:
            if rule.search(path):
                hits.append(f'{path} [{label}]')
                break
    return hits


def scan_text_for_content(label_prefix: str, text: str) -> list[str]:
    hits: list[str] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for label, rule in CONTENT_RULES:
            if rule.search(line):
                hits.append(f'{label_prefix}:{line_no} [{label}]')
                break
    return hits


def non_authoritative_marker(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("nonAuthoritative") is True or data.get("authority") in {"sample", "stub", "non-authoritative"}:
        return True
    meta = data.get("meta")
    return isinstance(meta, dict) and (
        meta.get("nonAuthoritative") is True
        or meta.get("authority") in {"sample", "stub", "non-authoritative"}
    )


def stale_openclaw_config_hit(path: str, text: str) -> str | None:
    if path != "openclaw.json":
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # A non-JSON tracked openclaw.json would be ambiguous as config authority.
        return "openclaw.json [stale or malformed config snapshot]"
    if isinstance(data, dict):
        config_keys = OPENCLAW_CONFIG_KEYS & set(data)
        if config_keys:
            keys = ", ".join(sorted(config_keys))
            return f"openclaw.json [stale workspace config snapshot: {keys}]"
    if non_authoritative_marker(data):
        return None
    return None


def read_worktree_text(path: str) -> str | None:
    full = repo_root / path
    if not full.is_file():
        return None
    try:
        if full.stat().st_size > MAX_CONTENT_SCAN_BYTES:
            return None
        return full.read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return None


try:
    declared_paths = parse_declared_paths(sys.argv[1:])
except ValueError as exc:
    print(f"❌ Refusing to continue: {exc}", file=sys.stderr)
    sys.exit(2)

declared_scan_paths = expand_declared_paths(declared_paths)
tracked_files = get_lines('git', 'ls-files', '-z')
staged_files = get_lines('git', 'diff', '--cached', '--diff-filter=ACMRD', '--name-only', '-z')

tracked_hits = path_rule_hits(tracked_files)
staged_hits = path_rule_hits(staged_files)
declared_hits = path_rule_hits(declared_scan_paths)

if tracked_hits:
    print('❌ Refusing to continue: repo already tracks secret-prone/local-only paths:')
    for hit in tracked_hits[:50]:
        print(f'   - {hit}')
    if len(tracked_hits) > 50:
        print(f'   ... and {len(tracked_hits) - 50} more')
    print('   Clean these out of git history/index first, then try again.')
    sys.exit(1)

if staged_hits:
    print('❌ Refusing to continue: staged paths look secret-prone, raw, or local-only:')
    for hit in staged_hits[:50]:
        print(f'   - {hit}')
    if len(staged_hits) > 50:
        print(f'   ... and {len(staged_hits) - 50} more')
    print('   Unstage/remove them or expand .gitignore before pushing.')
    sys.exit(1)

if declared_hits:
    print('❌ Refusing to continue: declared paths look secret-prone, raw, or local-only:')
    for hit in declared_hits[:50]:
        print(f'   - {hit}')
    if len(declared_hits) > 50:
        print(f'   ... and {len(declared_hits) - 50} more')
    print('   Remove/redact them or choose a safer scoped path.')
    sys.exit(1)

# Always inspect tracked openclaw.json, if present, so a stale workspace config
# snapshot cannot sit quietly in the repo even when it is not staged.
stale_config_hits: list[str] = []
for path in sorted(set([p for p in tracked_files if p == "openclaw.json"] + [p for p in declared_scan_paths if p == "openclaw.json"])):
    text = read_worktree_text(path)
    if text is None:
        continue
    hit = stale_openclaw_config_hit(path, text)
    if hit:
        stale_config_hits.append(hit)

if stale_config_hits:
    print('❌ Refusing to continue: workspace config snapshot is authoritative or stale:')
    for hit in stale_config_hits:
        print(f'   - {hit}')
    print('   Keep live config in the configured OpenClaw home and track only a non-authoritative stub/sample.')
    sys.exit(1)

patch_args = ['git', 'diff', '--cached', '--text', '--no-color', '--unified=0']
if staged_files:
    patch_args.extend(['--', *staged_files])
patch = subprocess.run(
    patch_args,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
).stdout.decode('utf-8', errors='ignore')

content_hits: list[str] = []
for raw_line in patch.splitlines():
    if not raw_line.startswith('+') or raw_line.startswith('+++ '):
        continue
    line = raw_line[1:]
    for label, rule in CONTENT_RULES:
        if rule.search(line):
            content_hits.append(f'staged diff [{label}]')
            break

for path in declared_scan_paths:
    if is_allowlisted(path):
        continue
    text = read_worktree_text(path)
    if text is None:
        continue
    content_hits.extend(scan_text_for_content(path, text))

if content_hits:
    seen: list[str] = []
    for label in content_hits:
        if label not in seen:
            seen.append(label)
    print('❌ Refusing to continue: staged/declared content matches secret/token signatures:')
    for label in seen[:50]:
        print(f'   - {label}')
    if len(seen) > 50:
        print(f'   ... and {len(seen) - 50} more')
    print('   Remove/redact the secret, rotate it if exposed, then commit again.')
    sys.exit(1)

print(f'✅ Git preflight passed for {repo_root} (staged={len(staged_files)} declared={len(declared_scan_paths)})')
PY
