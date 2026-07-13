#!/usr/bin/env python3
"""Read-only runtime preflight for OpenClaw runtime-hardening waves.

The script has two jobs:

1. classify every command before execution and only run commands mechanically known
   to be read-only; and
2. fail closed if the observed route is not the approved OpenClaw Pi route for
   ``openai-codex/gpt-5.5``.

It intentionally does not repair config, restart services, refresh plugins, pair
nodes, rebuild sandboxes, index memory, or migrate native Codex runtimes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

EXPECTED_MODEL = "openai-codex/gpt-5.5"
EXPECTED_PROVIDER = "openai-codex"
EXPECTED_MODEL_NAME = "gpt-5.5"
EXPECTED_RUNTIME_ID = "pi"
EXPECTED_AGENT = "codex"

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.path_config import workspace_root

NON_AUTHORITATIVE_WORKSPACE_CONFIG = workspace_root() / "openclaw.json"

READ_ONLY = "READ_ONLY"
REPO_MUTATION = "REPO_MUTATION"
LIVE_CONFIG_MUTATION = "LIVE_CONFIG_MUTATION"
SERVICE_OR_EXPOSURE_MUTATION = "SERVICE_OR_EXPOSURE_MUTATION"
FORBIDDEN_NOT_RUN = "FORBIDDEN_NOT_RUN"

_RUNNABLE_CLASSIFICATIONS = {READ_ONLY}
_SHELL_WRAPPER_COMMANDS = {"sh", "bash", "dash", "zsh", "fish", "ksh", "csh", "tcsh", "pwsh", "powershell"}
_SHELL_CONTROL_TOKENS = {"&&", "||", ";", ";;", "|", "|&", "&"}
_REDIRECTION_TOKEN_RE = re.compile(r"^(?:\d*)?(?:>>?|<<?|<<<|<>|>&|<&|&>|&>>)(?:.*)?$|^(?:\d*)>&\d+$")


@dataclass(frozen=True)
class CommandClassification:
    classification: str
    reason: str

    @property
    def runnable(self) -> bool:
        return self.classification in _RUNNABLE_CLASSIFICATIONS


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    purpose: str


@dataclass(frozen=True)
class CommandRun:
    argv: tuple[str, ...]
    classification: str
    reason: str
    purpose: str
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "classification": self.classification,
            "reason": self.reason,
            "purpose": self.purpose,
            "returncode": self.returncode,
            "skipped": self.skipped,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


@dataclass(frozen=True)
class RouteGuardResult:
    accepted: bool
    reasons: tuple[str, ...]
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"accepted": self.accepted, "reasons": list(self.reasons), "summary": self.summary}


def _base_command(argv: Sequence[str]) -> str:
    if not argv:
        return ""
    return Path(argv[0]).name


def _contains_any(args: Iterable[str], needles: Iterable[str]) -> bool:
    lowered = {arg.lower() for arg in args}
    return any(needle in lowered for needle in needles)


def _shell_control_token(args: Iterable[str]) -> str | None:
    """Return the first shell control/redirection token found in argv.

    The preflight executes commands without a shell, but checker input may still
    present shell-like token streams. Those must fail closed before any command
    allowlist is considered so a safe prefix cannot hide a mutating suffix.
    """

    for raw in args:
        token = str(raw)
        if token in _SHELL_CONTROL_TOKENS or _REDIRECTION_TOKEN_RE.match(token):
            return token
    return None


def _normalize_agent(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower()


def _path_matches(path_text: str, target: Path) -> bool:
    stripped = path_text.strip().strip("'\"")
    if not stripped:
        return False
    candidate = Path(stripped).expanduser()
    if not candidate.is_absolute():
        return False
    try:
        return candidate.resolve(strict=False) == target
    except OSError:
        return os.path.normpath(str(candidate)) == str(target)


def _mentions_non_authoritative_workspace_config(args: Iterable[str]) -> bool:
    """Return true when a command relies on the stale workspace config snapshot.

    Live config truth for runtime hardening must come from OpenClaw config/model
    tooling. The workspace ``openclaw.json`` path can exist as repo data, but it
    must not be used as preflight evidence for live runtime configuration.
    """

    for raw in args:
        text = str(raw)
        candidates = [text]
        if "=" in text:
            candidates.append(text.split("=", 1)[1])
        if any(_path_matches(candidate, NON_AUTHORITATIVE_WORKSPACE_CONFIG) for candidate in candidates):
            return True
    return False


def _looks_like_native_route_migration(args: Iterable[str]) -> bool:
    lowered = [str(arg).lower() for arg in args]
    mentions_migration = any("migrat" in arg for arg in lowered)
    mentions_native_codex = any("native" in arg or "codex" in arg for arg in lowered)
    mentions_route_surface = any(arg in {"route", "routes", "model", "models", "codex"} or "route" in arg for arg in lowered)
    return mentions_migration and mentions_native_codex and mentions_route_surface


def _openclaw_classification(args: Sequence[str]) -> CommandClassification:
    if len(args) < 2:
        return CommandClassification(FORBIDDEN_NOT_RUN, "openclaw command without subcommand is not a read-only preflight command")

    subcommand = args[1].lower()
    lowered = [arg.lower() for arg in args]

    if _looks_like_native_route_migration(args):
        return CommandClassification(FORBIDDEN_NOT_RUN, "native Codex route migration is hard-forbidden")

    if subcommand == "doctor":
        if "--fix" in lowered or "--repair" in lowered:
            return CommandClassification(FORBIDDEN_NOT_RUN, "doctor repair/fix is hard-forbidden")
        if "--deep" in lowered:
            return CommandClassification(FORBIDDEN_NOT_RUN, "doctor --deep is skipped unless installed-version proof shows read-only behavior")
        return CommandClassification(FORBIDDEN_NOT_RUN, "doctor is not part of the Wave 1 read-only command set")

    if subcommand == "update" and "--repair" in lowered:
        return CommandClassification(FORBIDDEN_NOT_RUN, "update --repair is hard-forbidden")

    if subcommand in {"plugin", "plugins"}:
        return CommandClassification(FORBIDDEN_NOT_RUN, "plugin registry/lifecycle commands are forbidden in runtime hardening preflight")

    if subcommand == "gateway" and _contains_any(lowered[2:], {"restart", "start", "stop", "reload"}):
        return CommandClassification(FORBIDDEN_NOT_RUN, "gateway lifecycle commands are service/exposure mutations")

    if subcommand in {"node", "nodes"}:
        if _contains_any(lowered[2:], {"pair", "pairing", "setup", "setup-code", "approve", "install"}):
            return CommandClassification(FORBIDDEN_NOT_RUN, "node pairing/setup commands are forbidden")
        return CommandClassification(FORBIDDEN_NOT_RUN, "node commands are outside the Wave 1 read-only command set")

    if subcommand in {"caddy", "edge", "public-edge"} or _contains_any(lowered, {"caddy"}):
        return CommandClassification(FORBIDDEN_NOT_RUN, "Caddy/public-edge commands are forbidden")

    if subcommand == "sandbox" and len(lowered) >= 3 and lowered[2] == "recreate":
        return CommandClassification(FORBIDDEN_NOT_RUN, "sandbox recreate is hard-forbidden")

    if subcommand == "memory" and len(lowered) >= 3 and lowered[2] == "status" and "--index" in lowered:
        return CommandClassification(FORBIDDEN_NOT_RUN, "memory status --deep --index is forbidden")

    if subcommand == "tasks" and _contains_any(lowered[2:], {"--apply", "apply", "maintenance"}):
        return CommandClassification(FORBIDDEN_NOT_RUN, "tasks maintenance/apply is outside read-only preflight")

    if subcommand == "models" and len(lowered) >= 3 and lowered[2] == "status" and "--json" in lowered:
        return CommandClassification(READ_ONLY, "model route inspection")

    if subcommand == "config" and _contains_any(lowered[2:], {"set", "patch", "apply", "edit", "write", "reset", "import"}):
        return CommandClassification(LIVE_CONFIG_MUTATION, "config mutation is not authorized")

    if subcommand == "config" and len(lowered) >= 3 and lowered[2] in {"file", "validate"}:
        return CommandClassification(READ_ONLY, "config read/validation")

    if subcommand == "status":
        return CommandClassification(READ_ONLY, "runtime status inspection")

    if subcommand == "sessions" and "--json" in lowered:
        return CommandClassification(READ_ONLY, "session route inspection")

    if subcommand == "hooks" and len(lowered) >= 3 and lowered[2] == "check" and "--json" in lowered:
        return CommandClassification(READ_ONLY, "hook status inspection")

    if subcommand == "cron" and len(lowered) >= 3 and lowered[2] in {"status", "list"} and "--json" in lowered:
        return CommandClassification(READ_ONLY, "cron status/list inspection")

    if subcommand == "security" and len(lowered) >= 3 and lowered[2] == "audit" and "--json" in lowered:
        return CommandClassification(READ_ONLY, "security audit inspection")

    if subcommand == "sandbox" and len(lowered) >= 3 and lowered[2] == "explain" and "--json" in lowered:
        return CommandClassification(READ_ONLY, "sandbox explanation inspection")

    if subcommand == "memory" and len(lowered) >= 3 and lowered[2] == "status" and "--json" in lowered and "--index" not in lowered:
        return CommandClassification(READ_ONLY, "memory status without index")

    return CommandClassification(FORBIDDEN_NOT_RUN, "openclaw command is not mechanically proven read-only for Wave 1")


def classify_command(argv: Sequence[str]) -> CommandClassification:
    """Classify a command before execution.

    Unknown OpenClaw/service commands fail closed instead of being guessed safe.
    """

    args = tuple(str(arg) for arg in argv)
    if not args:
        return CommandClassification(FORBIDDEN_NOT_RUN, "empty command")

    control_token = _shell_control_token(args)
    if control_token is not None:
        return CommandClassification(FORBIDDEN_NOT_RUN, f"shell control/redirection token is not allowed: {control_token}")

    if _mentions_non_authoritative_workspace_config(args):
        return CommandClassification(
            FORBIDDEN_NOT_RUN,
            "workspace openclaw.json is non-authoritative; use OpenClaw config/model tooling instead",
        )

    cmd = _base_command(args)
    lowered = [arg.lower() for arg in args]

    if cmd in _SHELL_WRAPPER_COMMANDS:
        return CommandClassification(FORBIDDEN_NOT_RUN, "shell wrapper commands are not allowed in preflight")

    if cmd == "openclaw":
        return _openclaw_classification(args)

    if cmd == "git":
        if len(lowered) >= 2 and lowered[1] in {"status", "diff", "rev-parse", "ls-files", "log", "remote", "show", "branch"}:
            return CommandClassification(READ_ONLY, "git inspection")
        if len(lowered) >= 2 and lowered[1] in {"add", "restore", "rm", "mv", "commit", "merge", "rebase", "checkout", "switch"}:
            return CommandClassification(REPO_MUTATION, "git working-tree/index mutation")
        if len(lowered) >= 2 and lowered[1] in {"push", "fetch", "pull"}:
            return CommandClassification(FORBIDDEN_NOT_RUN, "network git command is outside Wave 1 preflight")
        return CommandClassification(FORBIDDEN_NOT_RUN, "git command is not in the read-only allowlist")

    if args == ("scripts/git-preflight.sh", ".") or args == ("./scripts/git-preflight.sh", "."):
        return CommandClassification(READ_ONLY, "repository preflight inspection")

    if cmd in {"python", "python3"} and "-m" in lowered:
        module_index = lowered.index("-m") + 1
        module = lowered[module_index] if module_index < len(lowered) else ""
        if module == "unittest":
            return CommandClassification(READ_ONLY, "Python test validation command")
        if module == "py_compile":
            return CommandClassification(REPO_MUTATION, "Python bytecode cache validation may update ignored pycache")

    if cmd in {"rg", "grep", "find", "pwd", "ls", "cat", "sed", "awk", "head", "tail", "test"}:
        return CommandClassification(READ_ONLY, "filesystem inspection")

    if cmd in {"mkdir", "touch", "cp", "mv", "rm", "tee"}:
        return CommandClassification(REPO_MUTATION, "filesystem mutation")

    if cmd in {"systemctl", "service", "caddy"}:
        return CommandClassification(SERVICE_OR_EXPOSURE_MUTATION, "service/public-edge command")

    return CommandClassification(FORBIDDEN_NOT_RUN, "command is not in the Wave 1 read-only allowlist")


def build_preflight_commands(*, agent: str | None = None, include_session_route: bool = True) -> list[CommandSpec]:
    commands = [
        CommandSpec(("git", "status", "--short", "--branch"), "repo status"),
        CommandSpec(("git", "diff", "--check"), "whitespace/conflict-marker check"),
        CommandSpec(("scripts/git-preflight.sh", "."), "repo secret/hygiene preflight"),
        CommandSpec(("openclaw", "models", "status", "--json"), "default model route proof"),
    ]
    if agent:
        commands.append(CommandSpec(("openclaw", "models", "status", "--agent", agent, "--json"), "agent model route proof"))
    if include_session_route:
        commands.append(
            CommandSpec(
                ("openclaw", "sessions", "--all-agents", "--active", "60", "--limit", "10", "--json"),
                "Pi runtime route proof",
            )
        )
    return commands


def forbidden_command_examples() -> list[tuple[str, ...]]:
    return [
        ("openclaw", "doctor", "--fix"),
        ("openclaw", "doctor", "--repair"),
        ("openclaw", "doctor", "--deep"),
        ("openclaw", "update", "--repair"),
        ("openclaw", "models", "migrate", "--to", "native-codex"),
        ("openclaw", "codex", "migrate", "--runtime", "native"),
        ("openclaw", "config", "patch", "models.default=openai/gpt-4.1"),
        ("openclaw", "config", "apply", "candidate.json"),
        ("openclaw", "plugins", "registry", "--json"),
        ("openclaw", "gateway", "restart"),
        ("openclaw", "gateway", "stop"),
        ("openclaw", "gateway", "start"),
        ("openclaw", "channels", "enable", "webchat"),
        ("openclaw", "webhooks", "set", "https://example.invalid/hook"),
        ("openclaw", "dashboard", "expose", "--public"),
        ("openclaw", "browser", "expose", "--public"),
        ("openclaw", "sandbox", "recreate", "--all", "--force"),
        ("openclaw", "memory", "status", "--deep", "--index", "--json"),
        ("cat", str(NON_AUTHORITATIVE_WORKSPACE_CONFIG)),
        ("systemctl", "restart", "openclaw-gateway"),
    ]


def _run_command(spec: CommandSpec, *, cwd: Path, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> CommandRun:
    classification = classify_command(spec.argv)
    if not classification.runnable:
        return CommandRun(
            spec.argv,
            classification.classification,
            classification.reason,
            spec.purpose,
            None,
            skipped=True,
        )
    completed = runner(
        list(spec.argv),
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return CommandRun(
        spec.argv,
        classification.classification,
        classification.reason,
        spec.purpose,
        completed.returncode,
        completed.stdout,
        completed.stderr,
        skipped=False,
    )


def _read_json(path: str | None) -> Any:
    if not path:
        return None
    if _mentions_non_authoritative_workspace_config([path]):
        raise ValueError("workspace openclaw.json is non-authoritative; use OpenClaw config/model tooling output")
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _parse_json_output(run: CommandRun) -> Any:
    return json.loads(run.stdout)


def _provider_model(provider: Any, model: Any) -> str | None:
    if isinstance(provider, str) and isinstance(model, str) and provider.strip() and model.strip():
        return f"{provider.strip()}/{model.strip()}"
    return None


def _status_models(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("defaultModel", "resolvedDefault", "model", "resolvedModel"):
        if isinstance(payload.get(key), str):
            result[key] = payload[key]
    provider_model = _provider_model(payload.get("provider"), payload.get("model"))
    if provider_model:
        result["providerModel"] = provider_model
    return result


def _check_model_payload(payload: Any, label: str, reasons: list[str], summary: dict[str, Any]) -> None:
    models = _status_models(payload)
    summary[label] = models
    if not isinstance(payload, dict):
        reasons.append(f"{label}_status_not_object")
        return

    model_fields = [field for field in ("defaultModel", "resolvedDefault", "providerModel") if field in models]
    if not model_fields:
        reasons.append(f"{label}_missing_model_fields")
    for field in model_fields:
        if models[field] != EXPECTED_MODEL:
            reasons.append(f"{label}_{field}_mismatch:{models[field]}")

    allowed_mismatch = False
    for allowed_key in ("allowed", "allowedModels"):
        if allowed_key in payload:
            allowed = payload.get(allowed_key)
            summary[f"{label}.{allowed_key}"] = allowed
            if not isinstance(allowed, list) or [str(item) for item in allowed] != [EXPECTED_MODEL]:
                allowed_mismatch = True
    if allowed_mismatch:
        reasons.append(f"{label}_allowed_models_mismatch")

    fallbacks = payload.get("fallbacks") or payload.get("fallbackModels")
    if fallbacks:
        summary[f"{label}.fallbacks"] = fallbacks
        reasons.append(f"{label}_fallbacks_not_empty")


def _looks_like_pi_runtime(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return normalized == EXPECTED_RUNTIME_ID or "openclaw pi" in normalized or normalized == "pi default"


def _payload_has_pi_runtime(payload: Any) -> bool:
    if isinstance(payload, dict):
        runtime = payload.get("agentRuntime") or payload.get("runtime")
        if isinstance(runtime, dict) and _looks_like_pi_runtime(runtime.get("id")):
            return True
        if _looks_like_pi_runtime(runtime):
            return True
        for key in ("agentRuntimeId", "runtimeId", "runtime_id"):
            if _looks_like_pi_runtime(payload.get(key)):
                return True
        return any(_payload_has_pi_runtime(value) for value in payload.values())
    if isinstance(payload, list):
        return any(_payload_has_pi_runtime(item) for item in payload)
    return False


def _payload_agent(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("agent", "agentId", "agent_id", "agentName", "agent_name"):
        normalized = _normalize_agent(payload.get(key))
        if normalized:
            return normalized
    return None


def _runtime_model_evidence(payload: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(value: Any, inherited_agent: str | None = None) -> None:
        if isinstance(value, dict):
            current_agent = _payload_agent(value) or inherited_agent
            provider = value.get("provider")
            provider_model = _provider_model(provider, value.get("model"))
            direct_model = value.get("model") if isinstance(value.get("model"), str) else None
            matched_model = None
            requires_agent_model_proof = False
            if provider_model == EXPECTED_MODEL:
                matched_model = provider_model
            elif direct_model == EXPECTED_MODEL:
                matched_model = direct_model
            elif direct_model == EXPECTED_MODEL_NAME and not (isinstance(provider, str) and provider.strip()):
                matched_model = direct_model
                requires_agent_model_proof = True
            if matched_model is not None:
                found.append(
                    {
                        "model": matched_model,
                        "hasPiRuntime": _payload_has_pi_runtime(value),
                        "agent": current_agent,
                        "requiresAgentModelProof": requires_agent_model_proof,
                    }
                )
            for child in value.values():
                walk(child, current_agent)
        elif isinstance(value, list):
            for item in value:
                walk(item, inherited_agent)

    walk(payload)
    return found


def _agent_model_status_proves_expected_model(payload: Any, expected_agent: str | None) -> bool:
    if payload is None:
        return False
    if not isinstance(payload, dict):
        return False
    payload_agent = _payload_agent(payload)
    if expected_agent is not None and payload_agent != expected_agent:
        return False

    reasons: list[str] = []
    summary: dict[str, Any] = {}
    _check_model_payload(payload, "agent", reasons, summary)
    models = summary.get("agent")
    model_fields = ("defaultModel", "resolvedDefault", "providerModel")
    has_expected_resolution = isinstance(models, dict) and any(models.get(field) == EXPECTED_MODEL for field in model_fields)
    return has_expected_resolution and not reasons


def check_route_guard(
    model_status: Any,
    *,
    runtime_status: Any | None = None,
    agent_model_status: Any | None = None,
    expected_agent: str | None = EXPECTED_AGENT,
) -> RouteGuardResult:
    """Validate the exact approved model route and Pi runtime evidence."""

    reasons: list[str] = []
    expected_agent_normalized = _normalize_agent(expected_agent)
    summary: dict[str, Any] = {
        "expectedModel": EXPECTED_MODEL,
        "expectedRuntimeId": EXPECTED_RUNTIME_ID,
        "expectedAgent": expected_agent_normalized,
    }
    _check_model_payload(model_status, "default", reasons, summary)
    if agent_model_status is not None:
        _check_model_payload(agent_model_status, "agent", reasons, summary)
    agent_model_proves_expected = _agent_model_status_proves_expected_model(agent_model_status, expected_agent_normalized)
    summary["agentModelStatusProvesExpectedModel"] = agent_model_proves_expected

    if runtime_status is None:
        reasons.append("missing_pi_runtime_evidence")
        summary["hasPiRuntimeEvidence"] = False
        summary["hasExpectedModelPiRuntimeEvidence"] = False
        summary["hasExpectedAgentRuntimeEvidence"] = expected_agent_normalized is None
        summary["runtimeModelEvidence"] = []
    else:
        has_pi = _payload_has_pi_runtime(runtime_status)
        runtime_model_evidence = _runtime_model_evidence(runtime_status)
        has_expected_model_pi = any(
            entry.get("hasPiRuntime") is True
            and (expected_agent_normalized is None or entry.get("agent") == expected_agent_normalized)
            and (
                entry.get("model") == EXPECTED_MODEL
                or (
                    entry.get("model") == EXPECTED_MODEL_NAME
                    and entry.get("requiresAgentModelProof") is True
                    and expected_agent_normalized is not None
                    and agent_model_proves_expected
                )
            )
            for entry in runtime_model_evidence
        )
        summary["hasPiRuntimeEvidence"] = has_pi
        summary["hasExpectedModelPiRuntimeEvidence"] = has_expected_model_pi
        summary["hasExpectedAgentRuntimeEvidence"] = (
            expected_agent_normalized is None
            or any(entry.get("agent") == expected_agent_normalized for entry in runtime_model_evidence)
        )
        summary["runtimeModelEvidence"] = runtime_model_evidence
        if not has_pi:
            reasons.append("missing_pi_runtime_evidence")
        if not has_expected_model_pi:
            reasons.append("missing_expected_model_pi_runtime_evidence")

    return RouteGuardResult(not reasons, tuple(reasons), summary)


def run_preflight(
    *,
    cwd: Path,
    agent: str | None = None,
    runtime_json: Any | None = None,
    include_session_route: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    commands = build_preflight_commands(agent=agent, include_session_route=include_session_route and runtime_json is None)
    runs: list[CommandRun] = []
    if dry_run:
        for spec in commands:
            classification = classify_command(spec.argv)
            runs.append(
                CommandRun(spec.argv, classification.classification, classification.reason, spec.purpose, None, skipped=True)
            )
        return {"commands": [run.to_dict() for run in runs], "routeGuard": None, "accepted": True}

    for spec in commands:
        run = _run_command(spec, cwd=cwd)
        runs.append(run)
        if run.skipped or run.returncode != 0:
            return {"commands": [item.to_dict() for item in runs], "routeGuard": None, "accepted": False}

    model_status = _parse_json_output(runs[3])
    agent_status = None
    route_index = 4
    if agent:
        agent_status = _parse_json_output(runs[4])
        route_index = 5
    observed_runtime = runtime_json
    if observed_runtime is None and include_session_route:
        observed_runtime = _parse_json_output(runs[route_index])

    route = check_route_guard(
        model_status,
        runtime_status=observed_runtime,
        agent_model_status=agent_status,
        expected_agent=agent or EXPECTED_AGENT,
    )
    return {"commands": [run.to_dict() for run in runs], "routeGuard": route.to_dict(), "accepted": route.accepted}


def _cmd_classify(args: argparse.Namespace) -> int:
    classification = classify_command(args.command)
    print(json.dumps({"classification": classification.classification, "reason": classification.reason}, indent=2))
    return 0 if classification.runnable else 2


def _cmd_route_guard(args: argparse.Namespace) -> int:
    result = check_route_guard(
        _read_json(args.models_json),
        runtime_status=_read_json(args.runtime_json),
        agent_model_status=_read_json(args.agent_models_json),
        expected_agent=args.expected_agent,
    )
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0 if result.accepted else 1


def _cmd_run(args: argparse.Namespace) -> int:
    payload = run_preflight(
        cwd=Path(args.cwd).resolve(),
        agent=args.agent,
        runtime_json=_read_json(args.runtime_json),
        include_session_route=not args.no_session_route,
        dry_run=args.dry_run,
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("accepted") else 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    classify = subparsers.add_parser("classify", help="classify a command before execution")
    classify.add_argument("command", nargs=argparse.REMAINDER)
    classify.set_defaults(func=_cmd_classify)

    guard = subparsers.add_parser("route-guard", help="validate model and Pi runtime route evidence")
    guard.add_argument("--models-json", required=True, help="path to `openclaw models status --json` output")
    guard.add_argument("--agent-models-json", help="optional path to `openclaw models status --agent ... --json` output")
    guard.add_argument("--runtime-json", required=True, help="path to session/status JSON containing Pi runtime evidence")
    guard.add_argument("--expected-agent", default=EXPECTED_AGENT, help="agent id that must be tied to Pi runtime evidence")
    guard.set_defaults(func=_cmd_route_guard)

    run = subparsers.add_parser("run", help="run the read-only preflight command set")
    run.add_argument("--cwd", default=os.getcwd(), help="worktree path to inspect")
    run.add_argument("--agent", default=None, help="optional agent id for agent route proof")
    run.add_argument("--runtime-json", help="pre-captured session/status JSON for Pi evidence")
    run.add_argument("--no-session-route", action="store_true", help="do not run openclaw sessions for Pi evidence")
    run.add_argument("--dry-run", action="store_true", help="classify but do not execute commands")
    run.set_defaults(func=_cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command_name", None) == "classify" and args.command and args.command[0] == "--":
        args.command = args.command[1:]
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
