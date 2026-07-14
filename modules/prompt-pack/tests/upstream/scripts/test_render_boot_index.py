#!/usr/bin/env python3
"""Tests for the generated boot index."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "render-boot-index.py"


def load_module():
    spec = importlib.util.spec_from_file_location("render_boot_index", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


render_boot_index = load_module()


def o200k_token_count(text: str) -> int:
    try:
        import tiktoken
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised by missing test dependency.
        raise AssertionError("tiktoken is required for the boot payload budget guard") from exc
    return len(tiktoken.get_encoding("o200k_base").encode(text))


class RenderBootIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="openclaw-boot-index-")
        self.root = Path(self.tempdir.name)
        (self.root / "state" / "issues").mkdir(parents=True)
        (self.root / "docs" / "on-demand").mkdir(parents=True)
        (self.root / "WORKFLOW.md").write_text(
            "---\n"
            "states:\n"
            "  terminal: [Done, Cancelled, Canceled, Duplicate, Blocked]\n"
            "---\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_json(self, relpath: str, payload: object) -> None:
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def test_fresh_state_renders_compact_index_with_pointers_and_categories(self) -> None:
        self.write_json(
            "state/orchestrator.json",
            {
                "activeProject": "openclaw-overlay-v2",
                "phase": "planning",
                "status": "compact status",
                "activeIssueIds": [],
                "approvedIssueIds": [],
                "approvedEpicIds": [],
                "authorizedEpic": None,
                "authorizationScope": None,
                "runningIssues": [],
                "blockedIssueIds": [],
                "maxConcurrentWorkers": 4,
            },
        )
        self.write_json(
            "state/issues/fwk-overlay-v1-disposable-validation-20260613.json",
            {
                "id": "fwk-overlay-v1-disposable-validation-20260613",
                "title": "Anchor reset",
                "state": "Human Review",
                "kind": "task",
                "workerMode": "pipeline",
                "status": {
                    "currentClassification": "SCAFFOLD_ONLY_NOT_REAL_OVERLAY_V1",
                    "summary": "Scaffold only",
                    "nextHumanDecision": "First real module planning",
                },
                "allowedPaths": [
                    "STATE.md",
                    "state/orchestrator.json",
                    "state/issues/fwk-overlay-v1-disposable-validation-20260613.json",
                    "handoffs/openclaw-overlay-v1-report/example.md",
                ],
            },
        )

        index = render_boot_index.build_index(self.root, generated_at="2026-07-09T00:00:00Z")

        self.assertEqual(index["lastIndexedAt"], "2026-07-09T00:00:00Z")
        self.assertEqual(index["activeProject"], "openclaw-overlay-v2")
        self.assertEqual(index["trackedIssues"][0]["id"], "fwk-overlay-v1-disposable-validation-20260613")
        self.assertEqual(index["trackedIssues"][0]["compatStatus"], "blocked")
        self.assertEqual(index["authorizedWork"]["dispatchAuthorizedIssueIds"], [])
        categories = index["authorizedWork"]["trackedButNotDispatchAuthorized"][0]["categories"]
        self.assertIn("state/control-plane records", categories)
        self.assertIn("Overlay V1 packet/report/evidence handoff roots", categories)
        self.assertIn("state/boot-index.json", str(self.root / "state" / "boot-index.json"))
        self.assertIn("sourceFiles", index["_meta"])

    def test_empty_state_renders_no_tracked_issues(self) -> None:
        self.write_json(
            "state/orchestrator.json",
            {
                "activeProject": "(none)",
                "approvedIssueIds": [],
                "phase": "idle",
                "status": "No active project",
                "maxConcurrentWorkers": 4,
            },
        )

        index = render_boot_index.build_index(self.root, generated_at="2026-07-09T00:00:00Z")

        self.assertEqual(index["trackedIssues"], [])
        self.assertEqual(index["authorizedWork"]["dispatchAuthorizedIssueIds"], [])
        self.assertEqual(index["authorizedWork"]["trackedDispatchAuthorized"], [])
        self.assertEqual(index["authorizedWork"]["trackedButNotDispatchAuthorized"], [])

    def test_live_schema_authorized_issue_renders_dispatch_authorized(self) -> None:
        self.write_json(
            "state/orchestrator.json",
            {
                "activeIssue": "task-y",
                "activeIssueIds": ["task-y"],
                "activeProject": "phase2b-seeded-control-plane",
                "approvedEpicIds": [],
                "approvedIssueIds": ["task-y"],
                "authorizationFrozenAt": "2026-07-10T01:54:07Z",
                "authorizationProject": "phase2b-seeded-control-plane",
                "authorizationScope": "Seeded test window: only task-y may be worked.",
                "authorizationSource": "phase2b seeded scratch state",
                "authorizedEpic": None,
                "blockedIssueIds": [],
                "lastUpdatedAt": "2026-07-10T01:54:07Z",
                "maxConcurrentWorkers": 1,
                "phase": "executing",
                "runningIssues": [],
                "schemaVersion": 1,
                "status": "Seeded control-plane scenario.",
            },
        )
        self.write_json(
            "state/issues/task-y.json",
            {
                "id": "task-y",
                "title": "Seeded task Y authorized work",
                "state": "Todo",
                "kind": "task",
                "workerMode": "pipeline",
                "status": {"processState": "authorized"},
                "allowedPaths": [
                    "y_result.txt",
                    "state/issues/task-y.json",
                    "state/orchestrator.json",
                    "STATE.md",
                    "state/active-tasks.json",
                ],
            },
        )
        self.write_json(
            "state/issues/task-z.json",
            {
                "id": "task-z",
                "title": "Seeded task Z tracked but not authorized",
                "state": "Todo",
                "kind": "task",
                "workerMode": "pipeline",
                "status": {"processState": "present-not-authorized"},
                "allowedPaths": [
                    "z_result.txt",
                    "state/issues/task-z.json",
                    "state/orchestrator.json",
                    "STATE.md",
                    "state/active-tasks.json",
                ],
            },
        )

        index = render_boot_index.build_index(self.root, generated_at="2026-07-10T02:09:18Z")

        self.assertEqual(index["authorizedWork"]["dispatchAuthorizedIssueIds"], ["task-y"])
        self.assertEqual(index["authorizedWork"]["trackedDispatchAuthorized"][0]["issueId"], "task-y")
        self.assertIs(index["authorizedWork"]["trackedDispatchAuthorized"][0]["dispatchAuthorized"], True)
        self.assertEqual(index["authorizedWork"]["trackedButNotDispatchAuthorized"][0]["issueId"], "task-z")

    def test_unrecognized_authorization_shape_fails_loud(self) -> None:
        self.write_json(
            "state/orchestrator.json",
            {
                "activeProject": "legacy-shape",
                "authorizedWork": {"dispatchAuthorizedIssueIds": ["task-y"]},
                "phase": "executing",
                "status": "Legacy authorization shape only",
                "maxConcurrentWorkers": 1,
            },
        )
        self.write_json(
            "state/issues/task-y.json",
            {
                "id": "task-y",
                "title": "Seeded task Y authorized work",
                "state": "Todo",
                "kind": "task",
                "workerMode": "pipeline",
                "allowedPaths": ["y_result.txt"],
            },
        )

        with self.assertRaises(render_boot_index.BootIndexError) as caught:
            render_boot_index.build_index(self.root, generated_at="2026-07-10T02:09:18Z")

        self.assertIn("SCHEMA_MISMATCH", str(caught.exception))
        self.assertIn("approvedIssueIds", str(caught.exception))

    def test_malformed_state_fails_loud(self) -> None:
        (self.root / "state" / "orchestrator.json").write_text("{bad", encoding="utf-8")

        with self.assertRaises(render_boot_index.BootIndexError) as caught:
            render_boot_index.build_index(self.root)

        self.assertIn("malformed JSON", str(caught.exception))

    def test_stale_index_check_fails_with_regenerate_instruction(self) -> None:
        self.write_json(
            "state/orchestrator.json",
            {
                "activeProject": "openclaw-overlay-v2",
                "approvedIssueIds": [],
                "phase": "planning",
                "status": "old",
                "maxConcurrentWorkers": 4,
            },
        )
        output = self.root / "state" / "boot-index.json"
        render_boot_index.write_index(render_boot_index.build_index(self.root), output)
        self.write_json(
            "state/orchestrator.json",
            {
                "activeProject": "openclaw-overlay-v2",
                "approvedIssueIds": [],
                "phase": "planning",
                "status": "new",
                "maxConcurrentWorkers": 4,
            },
        )

        with self.assertRaises(render_boot_index.BootIndexError) as caught:
            render_boot_index.check_index(self.root, output)

        self.assertIn("stale generated boot index", str(caught.exception))
        self.assertIn("Regenerate with:", str(caught.exception))

    def test_three_issue_boot_payload_stays_under_o200k_budget(self) -> None:
        self.write_json(
            "state/orchestrator.json",
            {
                "activeProject": "phase2-closeout-budget",
                "phase": "executing",
                "status": "Budget fixture with one finished, one authorized, one tracked unauthorized issue.",
                "activeIssueIds": ["task-y"],
                "approvedIssueIds": ["task-y"],
                "approvedEpicIds": ["epic-1"],
                "authorizedEpic": "epic-1",
                "authorizationScope": "fixture",
                "runningIssues": [],
                "blockedIssueIds": [],
                "maxConcurrentWorkers": 1,
            },
        )
        common_issue = {
            "kind": "task",
            "workerMode": "pipeline",
            "allowedPaths": [
                "STATE.md",
                "state/orchestrator.json",
                "state/active-tasks.json",
            ],
        }
        self.write_json(
            "state/issues/task-x.json",
            {
                **common_issue,
                "id": "task-x",
                "title": "Complete issue",
                "state": "Done",
            },
        )
        self.write_json(
            "state/issues/task-y.json",
            {
                **common_issue,
                "id": "task-y",
                "title": "Dispatch authorized issue",
                "state": "Todo",
                "status": {"processState": "authorized"},
            },
        )
        self.write_json(
            "state/issues/task-z.json",
            {
                **common_issue,
                "id": "task-z",
                "title": "Tracked unauthorized issue",
                "state": "Todo",
                "status": {"processState": "tracked-unauthorized"},
            },
        )
        output = self.root / "state" / "boot-index.json"
        render_boot_index.write_index(
            render_boot_index.build_index(self.root, generated_at="2026-07-11T20:20:00Z"),
            output,
        )

        self.assertEqual(render_boot_index.check_index(self.root, output), None)
        payload = "".join(
            path.read_text(encoding="utf-8")
            for path in (
                output,
                REPO_ROOT / "BOOT.md",
                REPO_ROOT / "ORCHESTRATOR.md",
                REPO_ROOT / "WORKFLOW.md",
            )
        )

        self.assertLess(o200k_token_count(payload), 2950)


if __name__ == "__main__":
    unittest.main()
