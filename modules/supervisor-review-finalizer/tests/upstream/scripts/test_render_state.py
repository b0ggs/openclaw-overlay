from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.test_claim_scope_finalizer import claim_scope  # noqa: E402


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class RenderStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.original_sys_path = list(sys.path)
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        self.render_state = load_module("render_state_module", REPO_ROOT / "scripts" / "render-state.py")
        self.render_state.ROOT = self.root
        self.render_state.STATE_DIR = self.root / "state"
        (self.render_state.STATE_DIR / "runs").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        sys.path[:] = self.original_sys_path
        self.tmp.cleanup()

    def test_load_summary_reads_issue_summary_json(self) -> None:
        summary_path = self.render_state.STATE_DIR / "runs" / "issue-1.summary.json"
        summary_path.write_text(json.dumps({"branch": "feat/test", "headCommit": "abc123"}) + "\n", encoding="utf-8")

        summary = self.render_state.load_summary("issue-1")

        self.assertIsNotNone(summary)
        self.assertEqual(summary.get("branch"), "feat/test")
        self.assertEqual(summary.get("headCommit"), "abc123")

    def test_issue_line_surfaces_finalizer_status(self) -> None:
        line = self.render_state.issue_line(
            {
                "id": "issue-finalizer",
                "project": "(framework)",
                "state": "Done",
                "allowedPaths": ["scripts/render-state.py"],
                "status": {
                    "finalizer": {
                        "ok": True,
                        "subjectCommitOid": "abcdef0123456789",
                        "destinationRef": "refs/heads/main",
                        "changedPaths": ["scripts/render-state.py"],
                        "stagedPaths": ["scripts/render-state.py"],
                        "errors": [],
                        "defaultRefUpdated": True,
                        "publicationPermission": "approved",
                        "claimScope": claim_scope(
                            subject="issue-finalizer",
                            subjectCommitOid="abcdef0123456789",
                            destinationRef="refs/heads/main",
                        ),
                    }
                },
            }
        )

        self.assertIn("finalizer=PROVEN", line)
        self.assertIn("commit=abcdef012345", line)
        self.assertIn("dest=refs/heads/main", line)

    def test_issue_line_does_not_render_missing_claim_scope_as_passed(self) -> None:
        line = self.render_state.issue_line(
            {
                "id": "issue-finalizer-missing-claim",
                "project": "(framework)",
                "state": "Done",
                "allowedPaths": ["scripts/render-state.py"],
                "status": {
                    "finalizer": {
                        "ok": True,
                        "subjectCommitOid": "abcdef0123456789",
                        "destinationRef": "refs/heads/main",
                        "changedPaths": ["scripts/render-state.py"],
                        "stagedPaths": ["scripts/render-state.py"],
                        "errors": [],
                    }
                },
            }
        )

        self.assertIn("finalizer=failed", line)
        self.assertIn("missing status.finalizer.claimScope", line)
        self.assertIn("Blocked (source=Done)", line)
        self.assertNotIn("finalizer=passed", line)

    def test_issue_line_marks_invalid_human_review_as_blocked_source_state(self) -> None:
        line = self.render_state.issue_line(
            {
                "id": "issue-invalid-human-review",
                "project": "(framework)",
                "state": "Human Review",
                "finalizerRequired": True,
                "allowedPaths": ["scripts/render-state.py"],
                "status": {
                    "finalizer": {
                        "ok": True,
                        "status": "passed",
                        "subjectCommitOid": "abcdef0123456789",
                        "destinationRef": "local-only-human-review",
                        "changedPaths": ["scripts/render-state.py"],
                        "stagedPaths": ["scripts/render-state.py"],
                        "errors": [],
                    }
                },
            }
        )

        self.assertIn("Blocked (source=Human Review)", line)
        self.assertIn("finalizer=failed", line)
        self.assertNotIn("finalizer=passed", line)

    def test_issue_line_includes_loaded_summary_fields(self) -> None:
        summary_path = self.render_state.STATE_DIR / "runs" / "issue-2.summary.json"
        summary_path.write_text(
            json.dumps(
                {
                    "branch": "feature/summary",
                    "headCommit": "abcdef0123456789",
                    "lastEval": {"status": "pass"},
                    "lastReview": {"verdict": "approve"},
                    "research": {"phase": "candidate_generation", "laneCount": 2, "championId": "champ-1", "championMetric": 4.2},
                    "scoring": {"metric": "score", "best": 99.1},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        line = self.render_state.issue_line({"id": "issue-2", "state": "In Progress", "kind": "task", "workerMode": "optimize"})

        self.assertIn("branch=feature/summary", line)
        self.assertIn("commit=abcdef0", line)
        self.assertIn("eval=pass", line)
        self.assertIn("review=approve", line)
        self.assertIn("researchPhase=candidate_generation", line)
        self.assertIn("lanes=2", line)
        self.assertIn("champion=champ-1", line)
        self.assertIn("metric=4.2", line)
        self.assertIn("scoreMetric=score", line)
        self.assertIn("scoreBest=99.1", line)

    def test_state_invariant_warnings_expose_stale_render_mismatches(self) -> None:
        warnings = self.render_state.state_invariant_warnings(
            {"phase": "executing", "activeProject": "proj", "runningIssues": [], "blockedIssues": []},
            [{"id": "blocked-1", "state": "Blocked", "kind": "task"}],
        )

        rendered = "\n".join(warnings)
        self.assertIn("phase=executing", rendered)
        self.assertIn("orchestrator.blockedIssues", rendered)
        self.assertIn("state/issues Blocked ['blocked-1']", rendered)

    def test_main_marks_state_md_as_derived_promoted_output(self) -> None:
        self.render_state.STATE_DIR.mkdir(parents=True, exist_ok=True)
        (self.render_state.STATE_DIR / "issues").mkdir(parents=True, exist_ok=True)
        (self.render_state.STATE_DIR / "orchestrator.json").write_text(
            json.dumps({"phase": "ready", "activeProject": "(framework)", "runningIssues": [], "blockedIssues": []}) + "\n",
            encoding="utf-8",
        )

        self.assertEqual(self.render_state.main(), 0)
        rendered = (self.root / "STATE.md").read_text(encoding="utf-8")

        self.assertIn("authoritative: state/orchestrator.json + state/issues/*.json", rendered)
        self.assertIn("derived: STATE.md + state/active-tasks.json", rendered)
        self.assertIn("STATE.md is derived-promoted output", rendered)
        self.assertIn("Do not hand-edit", rendered)
        self.assertIn("finalizer-reviewed promotion", rendered)


if __name__ == "__main__":
    unittest.main()
