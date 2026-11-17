"""The command-line batch runner: design parsing, interleaving, manifests (browser mocked)."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))  # also works as `-m unittest tests.x`
from recpath import batch  # noqa: E402
from recpath import browser_observation as bo  # noqa: E402
from recpath.storage import session_to_csv  # noqa: E402


DESIGN = """query,location,repeats,rounds,location_cookie,exclude_policy,category,label
Online Safety Act,IE,2,,,,policy,
Online Safety Act,GB,1,3,,watched,policy,
,US,,,,,,
riots,US,2,,false,,,riots US url
"""


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def design(self, text: str = DESIGN) -> Path:
        path = self.dir / "design.csv"
        path.write_text(text, encoding="utf-8")
        return path

    def test_design_rows_become_conditions(self):
        conditions = batch.read_design(self.design())
        self.assertEqual(len(conditions), 3)  # the row without a query is skipped
        first, second, third = conditions
        self.assertEqual((first.query, first.location, first.repeats, first.rounds), ("Online Safety Act", "IE", 2, None))
        self.assertEqual(first.label, "Online Safety Act · IE")
        self.assertEqual((second.rounds, second.exclude_policy, second.category), (3, "watched", "policy"))
        self.assertIsNone(first.exclude_policy)
        self.assertIs(third.location_cookie, False)
        self.assertEqual(third.label, "riots US url")

    def test_invalid_designs_are_refused(self):
        for text, message in (
            ("query,repeats\nq,1\n", "location"),
            ("query,location\nq,FR\n", "location must be one of"),
            ("query,location,repeats\nq,IE,0\n", "repeats must be between"),
            ("query,location,location_cookie\nq,IE,maybe\n", "true or false"),
            ("query,location,exclude_policy\nq,IE,all\n", "exclude_policy must be one of"),
            ("query,location\n,IE\n", "no rows"),
        ):
            with self.subTest(text=text), self.assertRaisesRegex(batch.DesignError, message):
                batch.read_design(self.design(text))

    def test_runs_are_interleaved_by_repeat(self):
        plan = batch.plan_runs(batch.read_design(self.design()))
        self.assertEqual(
            [(run.condition.index, run.repeat) for run in plan],
            [(1, 1), (2, 1), (3, 1), (1, 2), (3, 2)],
        )

    def test_settings_follow_the_condition(self):
        first, second, third = batch.read_design(self.design())
        settings = batch.settings_for(first, rounds=5, pacing_ms=100)
        self.assertEqual((settings.region, settings.max_rounds, settings.slow_mo_ms), ("IE", 5, 100))
        self.assertTrue(settings.location_cookie)
        self.assertEqual(batch.settings_for(second, rounds=5, pacing_ms=100).max_rounds, 3)
        self.assertEqual(batch.settings_for(second, rounds=5, pacing_ms=100).exclude_policy, "watched")
        self.assertEqual(settings.exclude_policy, bo.BrowserObservationSettings().exclude_policy)
        self.assertFalse(batch.settings_for(third, rounds=5, pacing_ms=100).location_cookie)

    def test_run_batch_tags_sessions_and_writes_the_manifest(self):
        calls = []

        def fake_run(**kwargs):
            calls.append(kwargs)
            return {"session_id": f"s{len(calls)}", "status": "completed", "end_reason": "max_rounds",
                    "rounds": [{}, {}], "error": None}

        conditions = batch.read_design(self.design())
        with mock.patch.object(bo, "run_browser_observation", side_effect=fake_run):
            manifest = batch.run_batch(conditions, batch_id="pilot", design_path=Path("design.csv"), rounds=2,
                                       pause_s=0, pacing_ms=0, batch_dir=self.dir, out=io.StringIO())
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[1]["batch"], {"batch_id": "pilot", "condition": 2, "repeat": 1,
                                             "label": "Online Safety Act · GB", "category": "policy"})
        self.assertEqual(calls[1]["settings"].region, "GB")
        saved = json.loads((self.dir / "pilot.json").read_text())
        self.assertEqual(saved, manifest)
        self.assertEqual([run["session_id"] for run in saved["runs"]], ["s1", "s2", "s3", "s4", "s5"])
        self.assertIsNotNone(saved["finished_at"])
        self.assertIn("code_version", saved)  # None outside a git checkout

    def test_a_browser_that_cannot_start_ends_the_batch(self):
        conditions = batch.read_design(self.design())
        with mock.patch.object(bo, "run_browser_observation", side_effect=bo.BrowserObservationError("no browser")):
            manifest = batch.run_batch(conditions, batch_id="pilot", design_path=Path("design.csv"), rounds=2,
                                       pause_s=0, pacing_ms=0, batch_dir=self.dir, out=io.StringIO())
        self.assertEqual(len(manifest["runs"]), 1)
        self.assertEqual(manifest["runs"][0]["end_reason"], "browser_failed")

    def test_dry_run_and_bad_batch_ids(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(batch.main([str(self.design()), "--dry-run"]), 0)
        self.assertIn("[5/5] riots US url · repeat 2", out.getvalue())
        with mock.patch("sys.stderr", io.StringIO()), mock.patch.object(bo, "playwright_available", return_value=True):
            self.assertEqual(batch.main([str(self.design()), "--batch-id", "../x"]), 2)

    def test_csv_carries_the_applied_location_and_batch(self):
        session = {
            "session_id": "s1", "mode": "browser_observation", "region": "GB", "batch": {"batch_id": "pilot"},
            "rounds": [{"round": 1, "location": {"requested": "GB", "gl": "GB"},
                        "raw_results": [{"video_id": "a", "label": "normal_video"}]}],
        }
        header, row = session_to_csv(session).splitlines()[:2]
        values = dict(zip(header.split(","), row.split(",")))
        self.assertEqual((values["region"], values["location_applied"], values["batch_id"]), ("GB", "GB", "pilot"))


if __name__ == "__main__":
    unittest.main()
