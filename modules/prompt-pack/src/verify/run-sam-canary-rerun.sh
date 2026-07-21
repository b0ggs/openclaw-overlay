#!/usr/bin/env bash
# Provenance-gated SAM canary rerun driver.
set -euo pipefail

DRIVER_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/$(basename "${BASH_SOURCE[0]}")"
export SAM_CANARY_RERUN_DRIVER_PATH="$DRIVER_PATH"

exec python3 - "$@" <<'PY'
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


RUNNER_INSTALL_PATH = "verify/run-sam-canary.sh"
REQUIRED_MARKERS = ("trusted_infra_text", "infra_invalid_reasons")
FULL_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PreflightBlocked(Exception):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def emit_json(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run_cmd(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def require_sha256(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if not SHA256_RE.fullmatch(normalized):
        raise PreflightBlocked(f"{label} must be a full 64-character lowercase hex SHA-256")
    return normalized


def require_commit(value: str) -> str:
    normalized = value.strip().lower()
    if not FULL_COMMIT_RE.fullmatch(normalized):
        raise PreflightBlocked("--overlay-commit must be a full 40-character lowercase hex commit")
    return normalized


def reject_inline_credentials(repo_value: str) -> None:
    parsed = urlsplit(repo_value)
    if parsed.scheme in {"http", "https"} and (parsed.username or parsed.password):
        raise PreflightBlocked("overlay repo URL must not contain inline credentials")


def ensure_fresh_work_root(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.exists() and any(path.iterdir()):
        raise PreflightBlocked(f"work root must be absent or empty: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_fresh_execution_workspace(path: Path, work_root: Path) -> Path:
    path = path.expanduser().resolve()
    work_root = work_root.resolve()
    try:
        path.relative_to(work_root)
    except ValueError as exc:
        raise PreflightBlocked(f"execution workspace must be under work root: {path}") from exc
    if path.exists() and any(path.iterdir()):
        raise PreflightBlocked(f"execution workspace must be absent or empty: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def validate_identity(args: argparse.Namespace) -> dict[str, Any]:
    repo_mode = bool(args.overlay_repo or args.overlay_commit)
    archive_mode = bool(args.archive or args.archive_sha256)
    if repo_mode == archive_mode:
        raise PreflightBlocked(
            "provide exactly one explicit overlay identity: "
            "--overlay-repo with full --overlay-commit, or --archive with --archive-sha256"
        )
    if repo_mode:
        if not args.overlay_repo or not args.overlay_commit:
            raise PreflightBlocked("--overlay-repo requires a full --overlay-commit")
        reject_inline_credentials(args.overlay_repo)
        commit = require_commit(args.overlay_commit)
        return {"kind": "git", "overlayRepo": args.overlay_repo, "overlayCommit": commit}
    if not args.archive or not args.archive_sha256:
        raise PreflightBlocked("--archive requires --archive-sha256")
    archive = args.archive.expanduser().resolve()
    if not archive.is_file():
        raise PreflightBlocked(f"archive does not exist: {archive}")
    expected = require_sha256(args.archive_sha256, "--archive-sha256")
    actual = sha256_file(archive)
    if actual != expected:
        raise PreflightBlocked(f"archive SHA-256 mismatch: expected {expected}, got {actual}")
    return {"kind": "archive", "archive": str(archive), "archiveSha256": actual}


def acquire_git_source(identity: dict[str, Any], work_root: Path, summary: dict[str, Any]) -> Path:
    repo_value = str(identity["overlayRepo"])
    local_candidate = Path(repo_value).expanduser()
    repo_arg = str(local_candidate.resolve()) if local_candidate.exists() else repo_value
    source_dir = work_root / "overlay-source"
    clone = run_cmd(["git", "clone", "--no-checkout", repo_arg, str(source_dir)], cwd=work_root)
    summary["sourceAcquisition"] = {
        "method": "git-clone",
        "source": repo_value,
        "cloneExit": clone.returncode,
        "sourceDir": str(source_dir),
    }
    if clone.returncode != 0:
        raise PreflightBlocked("git clone failed for explicit overlay repo")
    commit = str(identity["overlayCommit"])
    exists = run_cmd(["git", "-C", str(source_dir), "cat-file", "-e", f"{commit}^{{commit}}"], cwd=work_root)
    if exists.returncode != 0:
        raise PreflightBlocked(f"overlay commit is not present in source repo: {commit}")
    checkout = run_cmd(["git", "-C", str(source_dir), "checkout", "--detach", commit], cwd=work_root)
    summary["sourceAcquisition"].update(
        {
            "checkoutExit": checkout.returncode,
        }
    )
    if checkout.returncode != 0:
        raise PreflightBlocked("git checkout failed for explicit overlay commit")
    actual = run_cmd(["git", "-C", str(source_dir), "rev-parse", "HEAD"], cwd=work_root)
    actual_commit = actual.stdout.strip().lower()
    summary["sourceAcquisition"]["actualCommit"] = actual_commit
    if actual_commit != commit:
        raise PreflightBlocked(f"checked out wrong overlay commit: expected {commit}, got {actual_commit}")
    return source_dir


def require_safe_member_path(name: str, target: Path) -> Path:
    target_resolved = target.resolve()
    candidate = (target / name).resolve()
    try:
        candidate.relative_to(target_resolved)
    except ValueError as exc:
        raise PreflightBlocked(f"archive member escapes extraction root: {name}") from exc
    return candidate


def extract_zip_safely(archive: Path, target: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            target_path = require_safe_member_path(info.filename, target)
            unix_mode = info.external_attr >> 16
            file_type = unix_mode & 0o170000
            if info.is_dir():
                target_path.mkdir(parents=True, exist_ok=True)
                continue
            if file_type not in {0, 0o100000}:
                raise PreflightBlocked(f"archive member type not allowed: {info.filename}")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, target_path.open("wb") as dest:
                shutil.copyfileobj(source, dest)
            if unix_mode:
                target_path.chmod(unix_mode & 0o777)


def extract_tar_safely(archive: Path, target: Path) -> None:
    with tarfile.open(archive) as tf:
        for member in tf.getmembers():
            target_path = require_safe_member_path(member.name, target)
            if member.isdir():
                target_path.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isreg():
                raise PreflightBlocked(f"archive member type not allowed: {member.name}")
            source = tf.extractfile(member)
            if source is None:
                raise PreflightBlocked(f"archive regular file could not be read: {member.name}")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with source, target_path.open("wb") as dest:
                shutil.copyfileobj(source, dest)
            target_path.chmod(member.mode & 0o777)


def acquire_archive_source(identity: dict[str, Any], work_root: Path, summary: dict[str, Any]) -> Path:
    archive = Path(str(identity["archive"]))
    extract_dir = work_root / "overlay-source"
    extract_dir.mkdir(parents=True, exist_ok=False)
    if zipfile.is_zipfile(archive):
        extract_zip_safely(archive, extract_dir)
        method = "zip"
    elif tarfile.is_tarfile(archive):
        extract_tar_safely(archive, extract_dir)
        method = "tar"
    else:
        raise PreflightBlocked(f"unsupported archive type: {archive}")
    candidates = [
        path.parent.parent.parent
        for path in extract_dir.rglob("modules/prompt-pack/install.sh")
        if path.is_file()
    ]
    source_dir = candidates[0] if candidates else extract_dir
    summary["sourceAcquisition"] = {
        "method": f"archive-{method}",
        "archive": str(archive),
        "archiveSha256": identity["archiveSha256"],
        "sourceDir": str(source_dir),
    }
    if not (source_dir / "modules" / "prompt-pack" / "install.sh").is_file():
        raise PreflightBlocked("archive does not contain modules/prompt-pack/install.sh")
    return source_dir


def parse_module_runner_sha(module_yaml: Path) -> str | None:
    if not module_yaml.is_file():
        return None
    in_files = False
    current: dict[str, str] | None = None
    text = module_yaml.read_text(encoding="utf-8")
    for raw_line in text.splitlines():
        if raw_line.startswith("files:"):
            in_files = True
            continue
        if raw_line and not raw_line.startswith((" ", "-")) and not raw_line.startswith("files:"):
            if current and current.get("install_path") == RUNNER_INSTALL_PATH:
                return current.get("sha256")
            current = None
            in_files = False
        if not in_files:
            continue
        line = raw_line.rstrip()
        item = re.match(r"^\s*-\s+([A-Za-z0-9_]+):\s*(.*)$", line)
        field = re.match(r"^\s+([A-Za-z0-9_]+):\s*(.*)$", line)
        if item:
            if current and current.get("install_path") == RUNNER_INSTALL_PATH:
                return current.get("sha256")
            current = {item.group(1): item.group(2).strip().strip('"')}
        elif field and current is not None:
            current[field.group(1)] = field.group(2).strip().strip('"')
    if current and current.get("install_path") == RUNNER_INSTALL_PATH:
        return current.get("sha256")
    return None


def install_prompt_pack(source_dir: Path, execution_workspace: Path, summary: dict[str, Any]) -> None:
    install_script = source_dir / "modules" / "prompt-pack" / "install.sh"
    if not install_script.is_file():
        raise PreflightBlocked(f"prompt-pack installer missing: {install_script}")
    execution_workspace.mkdir(parents=True, exist_ok=True)
    proc = run_cmd(["bash", str(install_script), str(execution_workspace)], cwd=source_dir)
    summary["install"] = {
        "installer": str(install_script),
        "executionWorkspace": str(execution_workspace),
        "exitCode": proc.returncode,
    }
    if proc.returncode != 0:
        raise PreflightBlocked("prompt-pack install failed")


def verify_runner(
    *,
    source_dir: Path,
    execution_workspace: Path,
    expected_override: str | None,
    summary: dict[str, Any],
) -> tuple[Path, str]:
    runner = (execution_workspace / RUNNER_INSTALL_PATH).resolve()
    expected_source = "argument"
    if expected_override:
        expected = require_sha256(expected_override, "--expected-runner-sha256")
    else:
        metadata = parse_module_runner_sha(source_dir / "modules" / "prompt-pack" / "module.yaml")
        if not metadata:
            raise PreflightBlocked("module metadata does not declare verify/run-sam-canary.sh SHA-256")
        expected = require_sha256(metadata, "module metadata runner sha256")
        expected_source = "module.yaml"
    exists = runner.is_file()
    executable = os.access(runner, os.X_OK)
    actual = sha256_file(runner) if exists else None
    text = runner.read_text(encoding="utf-8", errors="replace") if exists else ""
    markers = {marker: marker in text for marker in REQUIRED_MARKERS}
    workspace_resolved = execution_workspace.resolve()
    try:
        runner.relative_to(workspace_resolved)
        path_under_workspace = True
    except ValueError:
        path_under_workspace = False
    summary["runnerProvenance"] = {
        "runnerPath": str(runner),
        "runnerExists": exists,
        "runnerExecutable": executable,
        "runnerSha256": actual,
        "expectedRunnerSha256": expected,
        "expectedRunnerSha256Source": expected_source,
        "expectedRunnerSha256Matches": actual == expected,
        "markers": markers,
        "pathUnderExecutionWorkspace": path_under_workspace,
    }
    if not exists:
        raise PreflightBlocked(f"installed runner missing: {runner}")
    if not executable:
        raise PreflightBlocked(f"installed runner is not executable: {runner}")
    if actual != expected:
        raise PreflightBlocked(f"runner SHA-256 mismatch: expected {expected}, got {actual}")
    missing = [marker for marker, present in markers.items() if not present]
    if missing:
        raise PreflightBlocked(f"installed runner missing required provenance markers: {', '.join(missing)}")
    if not path_under_workspace:
        raise PreflightBlocked("installed runner path is outside execution workspace")
    return runner, expected


def build_canary_command(
    *,
    runner: Path,
    execution_workspace: Path,
    canary_out_dir: Path,
    valid_trials: int,
    max_attempts_per_slot: int,
) -> list[str]:
    return [
        str(runner),
        "--workspace",
        str(execution_workspace.resolve()),
        "--out-dir",
        str(canary_out_dir.resolve()),
        "--valid-trials",
        str(valid_trials),
        "--max-attempts-per-slot",
        str(max_attempts_per_slot),
    ]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provenance-gated SAM canary rerun driver.")
    parser.add_argument("--overlay-repo", help="Local path or Git URL for the overlay repository.")
    parser.add_argument("--overlay-commit", help="Full 40-character overlay commit to check out.")
    parser.add_argument("--archive", type=Path, help="Overlay source archive.")
    parser.add_argument("--archive-sha256", help="Full 64-character SHA-256 for --archive.")
    parser.add_argument("--work-root", type=Path, help="Absent or empty root for this rerun's source/workspace/evidence.")
    parser.add_argument("--execution-workspace", type=Path, help="Workspace that receives modules/prompt-pack before verification.")
    parser.add_argument("--out-dir", type=Path, help="Directory for provenance.json; defaults under --work-root.")
    parser.add_argument("--canary-out-dir", type=Path, help="Evidence directory that would be passed to the canary runner.")
    parser.add_argument("--expected-runner-sha256", help="Override module.yaml runner SHA-256 metadata.")
    parser.add_argument("--valid-trials", type=int, default=1)
    parser.add_argument("--max-attempts-per-slot", type=int, default=3)
    parser.add_argument("--preflight-only", action="store_true", help="Verify provenance and stop before canary launch.")
    parser.add_argument("--no-launch", action="store_true", help="Alias for --preflight-only.")
    parser.add_argument("--allow-launch", action="store_true", help="Request launch after provenance; currently fails closed.")
    args = parser.parse_args(argv)
    if args.preflight_only and args.allow_launch:
        parser.error("--preflight-only and --allow-launch are mutually exclusive")
    if args.no_launch and args.allow_launch:
        parser.error("--no-launch and --allow-launch are mutually exclusive")
    if args.valid_trials < 1:
        parser.error("--valid-trials must be >= 1")
    if args.max_attempts_per_slot < 1:
        parser.error("--max-attempts-per-slot must be >= 1")
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    summary: dict[str, Any] = {
        "schemaVersion": 1,
        "generatedAt": utc_now(),
        "driverPath": os.environ.get("SAM_CANARY_RERUN_DRIVER_PATH"),
        "status": "STARTED",
        "preflightOnly": bool(args.preflight_only or args.no_launch or not args.allow_launch),
        "launchAttempted": False,
        "gatewayOrAgentLaunchAttempted": False,
    }
    try:
        identity = validate_identity(args)
        summary["overlayIdentity"] = identity
        if args.work_root is None:
            work_root = Path(tempfile.mkdtemp(prefix=f"openclaw-sam-canary-rerun-{utc_stamp()}-"))
        else:
            work_root = ensure_fresh_work_root(args.work_root)
        summary["workRoot"] = str(work_root)
        out_dir = args.out_dir.expanduser().resolve() if args.out_dir else work_root / "provenance"
        source_dir = acquire_git_source(identity, work_root, summary) if identity["kind"] == "git" else acquire_archive_source(identity, work_root, summary)
        execution_workspace = (
            args.execution_workspace.expanduser().resolve()
            if args.execution_workspace
            else work_root / "execution-workspace"
        )
        execution_workspace = ensure_fresh_execution_workspace(execution_workspace, work_root)
        summary["executionWorkspace"] = {
            "path": str(execution_workspace),
            "fresh": True,
            "underWorkRoot": True,
        }
        canary_out_dir = args.canary_out_dir.expanduser().resolve() if args.canary_out_dir else work_root / "canary-evidence"
        install_prompt_pack(source_dir, execution_workspace, summary)
        runner, expected = verify_runner(
            source_dir=source_dir,
            execution_workspace=execution_workspace,
            expected_override=args.expected_runner_sha256,
            summary=summary,
        )
        canary_command = build_canary_command(
            runner=runner,
            execution_workspace=execution_workspace,
            canary_out_dir=canary_out_dir,
            valid_trials=args.valid_trials,
            max_attempts_per_slot=args.max_attempts_per_slot,
        )
        summary["canaryCommand"] = canary_command
        summary["canaryCommandVerified"] = (
            Path(canary_command[0]).resolve() == runner
            and str(execution_workspace.resolve()) in canary_command
        )
        summary["runnerProvenance"]["verified"] = True
        summary["runnerProvenance"]["expectedRunnerSha256"] = expected
        if not summary["canaryCommandVerified"]:
            raise PreflightBlocked("canary command does not point at the verified execution workspace runner")
        if args.allow_launch:
            summary["status"] = "LAUNCH_NOT_IMPLEMENTED"
            summary["blockedReason"] = "live SAM canary launch is intentionally not implemented by this provenance driver"
            write_json(out_dir / "provenance.json", summary)
            emit_json(summary)
            return 4
        summary["status"] = "PREFLIGHT_OK"
        summary["launchStatus"] = "not_launched_preflight_only"
        write_json(out_dir / "provenance.json", summary)
        emit_json(summary)
        return 0
    except PreflightBlocked as exc:
        summary["status"] = "BLOCKED"
        summary["blockedReason"] = str(exc)
        out_dir = args.out_dir.expanduser().resolve() if args.out_dir else Path(summary.get("workRoot", tempfile.gettempdir())) / "provenance"
        write_json(out_dir / "provenance.json", summary)
        emit_json(summary)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
PY
