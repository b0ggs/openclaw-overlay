"""Shared OpenClaw path configuration helpers.

The portability contract keeps machine paths configurable through the same
``OPENCLAW_*`` environment variables used by repo-authority runtime policy.
Defaults preserve the current live-machine layout.
"""

from __future__ import annotations

import os
from pathlib import Path


def _configured_path(env_name: str, default: Path | str) -> Path:
    raw = os.environ.get(env_name)
    return Path(raw if raw else default).expanduser()


def openclaw_home(default: Path | str | None = None) -> Path:
    return _configured_path("OPENCLAW_HOME", default or (Path.home() / ".openclaw"))


def workspace_root(default: Path | str | None = None) -> Path:
    if os.environ.get("OPENCLAW_WORKSPACE_ROOT"):
        return _configured_path("OPENCLAW_WORKSPACE_ROOT", "")
    return Path(default).expanduser() if default is not None else openclaw_home() / "workspace"


def mds_root(default: Path | str | None = None) -> Path:
    if os.environ.get("OPENCLAW_MDS_ROOT"):
        return _configured_path("OPENCLAW_MDS_ROOT", "")
    return Path(default).expanduser() if default is not None else openclaw_home() / "repos" / "openclaw-mds"


def projects_root(default: Path | str | None = None) -> Path:
    if os.environ.get("OPENCLAW_PROJECTS_ROOT"):
        return _configured_path("OPENCLAW_PROJECTS_ROOT", "")
    return Path(default).expanduser() if default is not None else openclaw_home() / "projects"


def external_projects_root(default: Path | str | None = None) -> Path:
    if os.environ.get("OPENCLAW_EXTERNAL_PROJECTS_ROOT"):
        return _configured_path("OPENCLAW_EXTERNAL_PROJECTS_ROOT", "")
    return Path(default).expanduser() if default is not None else Path.home() / "projects"


def worktrees_root(default: Path | str | None = None) -> Path:
    if os.environ.get("OPENCLAW_WORKTREES_ROOT"):
        return _configured_path("OPENCLAW_WORKTREES_ROOT", "")
    return Path(default).expanduser() if default is not None else openclaw_home() / "worktrees"


def stop_file(default: Path | str | None = None) -> Path:
    if os.environ.get("OPENCLAW_STOP_FILE"):
        return _configured_path("OPENCLAW_STOP_FILE", "")
    return Path(default).expanduser() if default is not None else openclaw_home() / "STOP"


def run_artifacts_root(default: Path | str | None = None) -> Path:
    if os.environ.get("OPENCLAW_RUNS_ROOT"):
        return _configured_path("OPENCLAW_RUNS_ROOT", "")
    return Path(default).expanduser() if default is not None else Path.home() / "tmp" / "openclaw-runs"
