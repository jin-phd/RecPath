import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # also works as `-m unittest tests.x`
import session_fixtures  # noqa: E402
from recpath import library  # noqa: E402


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.sessions_dir = self.data / "sessions"
        self.screenshot_dir = self.data / "screenshots"
        self.samples = session_fixtures.write_sample_data(self.data)
        library.clear_cache()
        self.addCleanup(library.clear_cache)

    def summaries(self):
        return {item["session_id"]: item for item in library.index(self.sessions_dir)}

    def test_index_reads_every_schema_and_skips_broken_files(self):
        rows = library.index(self.sessions_dir)
        self.assertEqual(len(rows), len(self.samples))  # the corrupt file and the .tmp file are skipped
        created = [row["created_at"] for row in rows]
        self.assertEqual(created, sorted(created, reverse=True))  # newest first

    def test_legacy_browser_session_summary(self):
        summary = self.summaries()[self.samples["legacy_browser"]["session_id"]]
        self.assertEqual(summary["mode"], library.MODE_CHAIN)
        self.assertEqual(summary["status"], library.LEGACY_STATUS)
        self.assertEqual(summary["device"], "iPhone 13")
        self.assertEqual(summary["consent"], "not recorded")
        self.assertEqual(summary["rounds"], 2)
        self.assertIsNone(summary["max_rounds"])
        self.assertEqual(summary["shorts"], 2)
        self.assertIsNone(summary["ads"])  # nothing about ads was recorded
        self.assertEqual(summary["first_selected_id"], "legacyA1")
        self.assertEqual(summary["first_thumbnail"], "https://i.ytimg.com/vi/legacyA1/mqdefault.jpg")
        self.assertEqual(summary["last_selected_id"], "legacyB1")
        self.assertEqual(summary["schema_version"], 1)

    def test_api_session_without_mode(self):
        summary = self.summaries()[self.samples["legacy_api"]["session_id"]]
        self.assertEqual(summary["mode"], library.MODE_API)
        self.assertEqual(summary["query"], "Irish teen gaming")
        self.assertIsNone(summary["device"])
        self.assertEqual(summary["consent"], "n/a")
        self.assertEqual(summary["strategy"], "random")

    def test_v2_and_v3_summaries(self):
        summaries = self.summaries()
        v2 = summaries[self.samples["v2"]["session_id"]]
        self.assertEqual(v2["status"], "completed")
        self.assertEqual(v2["max_rounds"], 3)
        self.assertEqual(v2["device"], "Pixel 7")
        self.assertEqual(v2["consent"], "Reject all")
        self.assertEqual(v2["ads"], 0)  # label_counts recorded, no ad cards
        self.assertEqual(v2["normal_candidates"], 6)
        v3 = summaries[self.samples["v3_ads"]["session_id"]]
        self.assertEqual(v3["status"], "stopped")  # completed + end_reason stopped
        self.assertEqual(v3["ads"], 2)  # one feed ad card + one player ad
        self.assertTrue(library.has_ad_data(self.samples["v3_ads"]))

    def test_location_comes_from_settings_or_the_session(self):
        self.assertEqual(library.location_of({"settings": {"region": "GB"}, "region": "IE"}), "GB")
        self.assertEqual(library.location_of({"region": "IE"}), "IE")
        self.assertIsNone(library.location_of({}))
        self.assertIn("location", next(iter(self.summaries().values())))

    def test_shorts_feed_session(self):
        summary = self.summaries()[self.samples["shorts"]["session_id"]]
        self.assertEqual(summary["mode"], library.MODE_SHORTS)
        self.assertEqual(summary["items"], 3)
        self.assertEqual(summary["shorts"], 2)
        self.assertEqual(summary["ads"], 1)
        self.assertEqual(summary["first_selected_id"], "shortA")

    def test_children_and_branch_fields(self):
        parent_id = self.samples["v2"]["session_id"]
        children = library.children_of(parent_id, self.sessions_dir)
        self.assertEqual([item["session_id"] for item in children], [self.samples["branch"]["session_id"]])
        child = children[0]
        self.assertTrue(child["is_branch"])
        self.assertEqual(child["parent_round"], 2)
        self.assertEqual(library.children_of(self.samples["legacy_api"]["session_id"], self.sessions_dir), [])

    def test_load_session_and_unsafe_ids(self):
        session_id = self.samples["v2"]["session_id"]
        loaded = library.load_session(session_id, self.sessions_dir)
        self.assertEqual(loaded["session_id"], session_id)
        self.assertIsNone(library.load_session("missing", self.sessions_dir))
        self.assertIsNone(library.load_session("../sessions/x", self.sessions_dir))
        self.assertIsNone(library.load_session("", self.sessions_dir))

    def test_cache_follows_file_changes(self):
        session = self.samples["v2"]
        path = self.sessions_dir / f"{session['session_id']}.json"
        self.assertEqual(self.summaries()[session["session_id"]]["query"], "Transition Year vlog Ireland")
        session["start_query"] = "changed query"
        path.write_text(json.dumps(session), encoding="utf-8")
        later = time.time() + 5
        os.utime(path, (later, later))
        self.assertEqual(self.summaries()[session["session_id"]]["query"], "changed query")
        self.assertEqual(library.load_session(session["session_id"], self.sessions_dir)["start_query"], "changed query")
        path.unlink()
        self.assertNotIn(session["session_id"], self.summaries())

    def test_round_screenshots_with_legacy_fallback(self):
        legacy_round = self.samples["legacy_browser"]["rounds"][0]
        # Stored as data/screenshots/... relative to the project root; found by name in the data folder.
        shots = library.round_screenshots(legacy_round, self.screenshot_dir)
        self.assertEqual(set(shots), {"landing"})
        self.assertTrue(shots["landing"].is_file())
        v2_round = self.samples["v2"]["rounds"][1]
        shots = library.round_screenshots(v2_round, self.screenshot_dir)
        self.assertEqual(set(shots), {"landing", "selected"})
        self.assertEqual(library.round_screenshots({"screenshots": {"landing": "nowhere.jpg"}}, self.screenshot_dir), {})
        self.assertIsNone(library.resolve_screenshot(None))
        self.assertIsNone(library.resolve_screenshot(42))

    def test_overlap_helpers(self):
        session = self.samples["v2"]
        self.assertEqual(library.selected_ids(session), ["v2A1", "v2B1"])
        per_round = library.recommended_ids(session)
        self.assertEqual(per_round[0], {"v2A1", "v2A2", "sharedX"})
        with_shorts = library.recommended_ids(session, labels=("normal_video", "short"))
        self.assertIn("short10", with_shorts[0])
        self.assertEqual(library.distinct_channels(session, picks_only=True), 1)

    def test_tolerates_odd_shapes(self):
        odd = {"session_id": "session_odd", "rounds": [None, {"round": 1, "results": None, "selected_result": "x"}],
               "parent": "not a dict", "settings": None, "consent": None}
        summary = library.summarize(odd)
        self.assertEqual(summary["rounds"], 1)
        self.assertFalse(summary["is_branch"])
        self.assertEqual(summary["last_selected_title"], "")
        self.assertEqual(library.label_counts({"label_counts": {}, "results": [{"label": "short"}]}), {"short": 1})

    def test_orphaned_running_status_is_shown_as_unfinished(self):
        summary = self.summaries()[self.samples["orphaned"]["session_id"]]
        self.assertEqual(summary["status"], "running")  # the summary keeps what is on disk
        self.assertEqual(library.display_status("running", summary["session_id"], None), library.UNFINISHED_STATUS)
        self.assertEqual(library.display_status("running", summary["session_id"], "session_other"),
                         library.UNFINISHED_STATUS)
        self.assertEqual(library.display_status("running", summary["session_id"], summary["session_id"]), "running")
        self.assertEqual(library.display_status("completed", summary["session_id"], None), "completed")
        self.assertEqual(library.display_status(None, "x", None), library.LEGACY_STATUS)

    def test_ad_counts_say_what_they_cover(self):
        summaries = self.summaries()
        self.assertEqual(summaries[self.samples["v2"]["session_id"]]["ads_scope"], library.ADS_SCOPE_FEED_ONLY)
        self.assertEqual(summaries[self.samples["v3_ads"]["session_id"]]["ads_scope"], library.ADS_SCOPE_FEED_AND_PLAYER)
        self.assertEqual(summaries[self.samples["shorts"]["session_id"]]["ads_scope"], library.ADS_SCOPE_SHORTS)
        self.assertIsNone(summaries[self.samples["legacy_browser"]["session_id"]]["ads_scope"])

    def test_export_safe_lets_an_odd_session_be_exported(self):
        from recpath import storage

        odd = {"session_id": "session_odd", "parent": "not a dict", "settings": None,
               "rounds": ["not-a-round", None,
                          {"round": 1, "results": None, "raw_results": [{"video_id": "a", "label": "normal_video"}, "x"],
                           "selected_result": "x", "selection": None, "navigation": "tap",
                           "filtered_out_video_ids": None}]}
        with self.assertRaises(Exception):
            storage.sessions_to_csv([odd])
        clean = library.export_safe(odd)
        csv_text = storage.sessions_to_csv([clean, library.export_safe(self.samples["v2"])])
        self.assertIn("session_odd", csv_text)
        self.assertEqual(odd["rounds"][0], "not-a-round")  # the original is not changed
        for session in self.samples.values():
            storage.session_to_csv(library.export_safe(session))


if __name__ == "__main__":
    unittest.main()
