"""Every page of the Streamlit app, run headless with streamlit.testing.AppTest on synthetic sessions."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent))  # also works as `-m unittest tests.x`
import session_fixtures  # noqa: E402
from recpath import browser_observation, library, paths, run_manager, storage  # noqa: E402

APP = Path(__file__).resolve().parent.parent / "app.py"


class UiPagesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.data = Path(cls.temp.name)
        cls.samples = session_fixtures.write_sample_data(cls.data)
        cls.patches = [
            mock.patch.object(paths, "SESSIONS_DIR", cls.data / "sessions"),
            mock.patch.object(paths, "SCREENSHOT_DIR", cls.data / "screenshots"),
            # A run started by a test must never write into the project's data/ folder.
            mock.patch.object(storage, "save_session", cls.save_to_temp),
        ]
        for patcher in cls.patches:
            patcher.start()

    @classmethod
    def tearDownClass(cls):
        for patcher in reversed(cls.patches):
            patcher.stop()
        cls.temp.cleanup()

    @classmethod
    def save_to_temp(cls, session, data_dir=None):
        path = cls.data / "sessions" / f"{session['session_id']}.json"
        path.write_text(json.dumps(session), encoding="utf-8")
        return path, path.with_suffix(".csv")

    def setUp(self):
        library.clear_cache()
        run_manager._reset_for_tests()
        self.addCleanup(run_manager._reset_for_tests)

    def app(self, page=None, **state):
        at = AppTest.from_file(str(APP), default_timeout=60)
        for key, value in state.items():
            at.session_state[key] = value
        at.run()
        if page:
            at.switch_page(page)
            at.run()
        self.assertNoException(at)
        return at

    def assertNoException(self, at):
        self.assertEqual([item.value for item in at.exception], [])

    def button(self, at, label):
        return next(item for item in at.button if item.label == label)

    def block_labels(self, node):
        """Labels of every block below ``node`` (expanders show up as "status" blocks in AppTest)."""
        labels = []
        for child in getattr(node, "children", {}).values():
            label = getattr(child, "label", None)
            if isinstance(label, str):
                labels.append(label)
            labels.extend(self.block_labels(child))
        return labels

    def texts(self, at):
        return " ".join(
            str(item.value)
            for kind in ("markdown", "caption", "info", "success", "warning", "error", "subheader", "title")
            for item in getattr(at, kind)
        )

    # ------------------------------------------------------------------ New run
    def test_new_run_page(self):
        at = self.app()
        self.assertEqual([item.value for item in at.title], ["New run"])
        at_tree = at._tree
        self.assertTrue(self.button(at, "Start run").disabled)
        self.assertIn("Enter a start query.", self.texts(at))
        at.text_input(key="w::start_query").input("funny cats").run()
        self.assertNoException(at)
        self.assertFalse(self.button(at, "Start run").disabled)
        expanders = self.block_labels(at_tree)
        self.assertIn("Need query ideas?", expanders)
        self.assertIn("Browser settings", expanders)
        self.assertIn("System prompt", [item.label for item in at.tabs])

    def test_new_run_api_mode_needs_a_key(self):
        at = self.app()
        with mock.patch.dict("os.environ", {"YOUTUBE_API_KEY": ""}):
            at.text_input(key="w::start_query").input("funny cats")
            at.segmented_control(key="w::run_mode").set_value(library.MODE_API).run()
        self.assertNoException(at)
        self.assertIn("API results per round", [item.label for item in at.slider])

    def test_start_runs_in_background_and_live_page_shows_the_outcome(self):
        def fake_runner(**kwargs):
            session = {"session_id": "session_20261022_090000_000099", "created_at": "2026-10-22T08:00:00Z",
                       "created_at_local": "2026-10-22 09:00:00 IST", "mode": "browser_observation",
                       "schema_version": 2, "status": "running", "start_query": kwargs["start_query"],
                       "settings": kwargs["settings"].public_dict(), "rounds": []}
            kwargs["on_event"]({"type": "started", "session_id": session["session_id"], "at": "2026-10-22T08:00:00Z",
                                "start_query": kwargs["start_query"]})
            record = session_fixtures.browser_round(1, ["liveA", "liveB"], screenshots={})
            session["rounds"].append(record)
            kwargs["on_frame"](session_fixtures._tiny_jpeg())
            kwargs["on_event"]({"type": "round", "session_id": session["session_id"], "round": 1, "record": record})
            session.update(status="completed", end_reason="max_rounds")
            kwargs["checkpoint"](session)
            kwargs["on_event"]({"type": "finished", "session_id": session["session_id"], "status": "completed",
                                "end_reason": "max_rounds", "error": None})
            return session

        with mock.patch.object(browser_observation, "run_browser_observation", fake_runner):
            at = self.app()
            at.text_input(key="w::start_query").input("funny cats").run()
            self.button(at, "Start run").click().run()
            self.assertNoException(at)
            handle = run_manager.latest()
            self.assertIsNotNone(handle)
            self.assertTrue(handle.join(10))
            self.assertEqual(handle.params["start_query"], "funny cats")
            at.switch_page("ui/pages/live_run.py").run()
            self.assertNoException(at)
            self.assertEqual([item.value for item in at.title], ["Live run"])
            self.assertTrue(any("Completed" in item.value for item in at.success))
            self.assertFalse(self.button(at, "Open in library").disabled)
            self.assertIn("funny cats", self.texts(at))

    def test_live_page_while_running(self):
        import threading

        release = threading.Event()

        def blocking_runner(**kwargs):
            kwargs["on_event"]({"type": "started", "session_id": "session_live", "at": "2026-10-22T08:00:00Z"})
            kwargs["on_event"]({"type": "step", "session_id": "session_live", "round": 1, "step": "scrolling",
                                "message": "Reading the feed", "at": "2026-10-22T08:00:01Z"})
            release.wait(10)
            return {"session_id": "session_live", "status": "completed", "end_reason": "stopped", "rounds": []}

        with mock.patch.object(browser_observation, "run_browser_observation", blocking_runner):
            handle = run_manager.start_chain(start_query="live query",
                                             settings=browser_observation.BrowserObservationSettings(max_rounds=2))
            try:
                at = self.app("ui/pages/live_run.py")
                self.assertIn("live query", self.texts(at))
                self.assertIn("Stop", [item.label for item in at.button])
                new_run = self.app()
                self.assertTrue(self.button(new_run, "Start run").disabled)
                self.assertIn("A run is in progress", self.texts(new_run))
            finally:
                release.set()
                handle.join(10)

    def test_live_page_idle(self):
        at = self.app("ui/pages/live_run.py")
        self.assertIn("Nothing is running right now.", self.texts(at))
        self.assertIn("Latest saved session", self.texts(at))

    # ------------------------------------------------------------------ Library
    def test_library_page_opens_every_kind_of_session(self):
        for name, session in self.samples.items():
            with self.subTest(name):
                at = self.app("ui/pages/library.py", library_selected=session["session_id"])
                tabs = [item.label for item in at.tabs]
                if name == "shorts":
                    # A Shorts feed has no rounds: no Timeline, Path graph or Candidates tabs.
                    self.assertEqual(tabs, ["Shorts feed", "Ads", "Raw JSON"])
                else:
                    for label in ("Timeline", "Path graph", "Candidates", "Ads", "Raw JSON"):
                        self.assertIn(label, tabs)
                self.assertTrue(at.subheader, "the session header is missing")

    def test_library_escapes_titles_and_lists_sessions(self):
        session = self.samples["legacy_browser"]
        at = self.app("ui/pages/library.py", library_selected=session["session_id"])
        self.assertEqual(at.subheader[0].value, r"Peppa Pig \# new \$episodes\$")
        table = at.dataframe[0].value
        self.assertEqual(len(table), len(self.samples))
        self.assertIn("Continue 5 more rounds", self.block_labels(at._tree))

    def test_library_without_selection_and_with_a_filter(self):
        at = self.app("ui/pages/library.py")
        self.assertIn("Select a session in the table to open it.", self.texts(at))
        at.text_input(key="w::lib_search").input("peppa").run()
        self.assertNoException(at)
        self.assertEqual(len(at.dataframe[0].value), 1)

    # ------------------------------------------------------------------ Compare
    def test_compare_page(self):
        at = self.app("ui/pages/compare.py")
        self.assertEqual([item.value for item in at.title], ["Compare sessions"])
        chosen = at.multiselect(key="w::compare_ids").value
        self.assertEqual(len(chosen), 2)
        queries = {library.load_session(value)["start_query"] for value in chosen}
        self.assertEqual(queries, {"Transition Year vlog Ireland"})
        self.assertIn("Overlap", [item.value for item in at.subheader])
        at.multiselect(key="w::compare_ids").set_value(
            [self.samples[name]["session_id"] for name in ("v2", "v3_ads", "legacy_browser")]
        ).run()
        self.assertNoException(at)

    def frame_with(self, at, column):
        return next(item.value for item in at.dataframe if column in item.value.columns)

    def test_compare_default_skips_branches_and_aligns_a_branch_with_its_parent(self):
        at = self.app("ui/pages/compare.py")
        chosen = at.multiselect(key="w::compare_ids").value
        self.assertNotIn(self.samples["branch"]["session_id"], chosen)  # a branch is no repeat of its parent
        parent, branch = self.samples["v2"]["session_id"], self.samples["branch"]["session_id"]
        at.multiselect(key="w::compare_ids").set_value([parent, branch]).run()
        self.assertNoException(at)
        overview = self.frame_with(at, "starts_at")
        self.assertEqual(list(overview["starts_at"]), ["R1", "R3 (from R2)"])
        # The branch (from round 2) starts at row 3: no row pairs its R1 with the parent's search page.
        rows = self.frame_with(at, "A–B")
        self.assertEqual(list(rows["round"]), ["R1", "R2", "R3"])
        self.assertTrue(rows["A–B"].isna().all())
        self.assertIn("Rows are aligned by depth", self.texts(at))

    # ------------------------------------------------------------------ Library: status, dialog
    def test_orphaned_running_session_is_shown_as_unfinished(self):
        session = self.samples["orphaned"]
        at = self.app("ui/pages/library.py", library_selected=session["session_id"])
        table = at.dataframe[0].value
        self.assertEqual(table.loc[table["session_id"] == session["session_id"], "status"].tolist(), ["unfinished"])
        self.assertNotIn("running", at.selectbox(key="w::lib_status").options)
        self.assertIn("This run never finished", self.texts(at))

    def test_round_dialog_shows_the_round_facts(self):
        session = self.samples["v2"]
        at = self.app("ui/pages/library.py", library_selected=session["session_id"])
        details = [item for item in at.button if item.label == "Details and branching"]
        details[1].click().run()  # round 2
        self.assertNoException(at)
        self.assertIn("Round facts", self.texts(at))
        facts = self.frame_with(at, "fact")
        values = dict(zip(facts["fact"], facts["value"]))
        self.assertEqual(values["Requested → landed video"], "v2B1 → v2B1")
        self.assertEqual(values["Next video opened by"], "tapped")
        self.assertEqual(values["Page URL"], "https://m.youtube.com/watch?v=v2B1")
        dialog_table = f"dialog_table_{session['session_id']}_2"
        cards = next(item.value for item in at.dataframe if item.key == dialog_table)
        self.assertEqual(list(cards["video_id"]), ["v2B1", "sharedX", "v2B3"])
        self.assertEqual(int(cards["picked"].sum()), 1)
        self.assertIn("Round record (JSON, without the item lists)", self.block_labels(at._tree))

    def test_legacy_chain_continuation_names_the_settings_it_will_use(self):
        session = self.samples["legacy_browser"]
        at = self.app("ui/pages/library.py", library_selected=session["session_id"])
        text = self.texts(at)
        self.assertIn("iPhone 13 ·", text)  # the session's own device, not the New run page's Pixel 7
        self.assertIn("cookies Accept all \\(New run page\\)", text)

    def test_max_rounds_flag_above_the_limit_is_clamped(self):
        with mock.patch.object(sys, "argv", ["streamlit", "--max-rounds", "12"]):
            at = self.app()
            self.assertEqual(at.number_input(key="w::rounds").value, 10)
            self.assertIn("--max-rounds 12 is above", self.texts(at))
        with mock.patch.object(sys, "argv", ["streamlit", "--max-rounds", "7"]):
            self.assertEqual(self.app().number_input(key="w::rounds").value, 7)


class CompareLabelTests(unittest.TestCase):
    """Two runs of one query started in the same minute (the case Compare exists for)."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        data = Path(cls.temp.name)
        (data / "sessions").mkdir()
        (data / "screenshots").mkdir()
        first = session_fixtures.v2_session(data / "screenshots")
        first.update(session_id="session_20261022_085613_440223", created_at="2026-10-22T08:56:13Z",
                     created_at_local="2026-10-22 09:56:13 IST", start_query="Irish rugby highlights")
        second = json.loads(json.dumps(first))
        second.update(session_id="session_20261022_085639_518876", created_at="2026-10-22T08:56:39Z",
                      created_at_local="2026-10-22 09:56:39 IST")
        second["rounds"][1]["raw_results"][0]["video_id"] = "onlyInSecond"
        third = session_fixtures.v3_ads_session()
        cls.ids = [first["session_id"], second["session_id"], third["session_id"]]
        for session in (first, second, third):
            (data / "sessions" / f"{session['session_id']}.json").write_text(json.dumps(session), encoding="utf-8")
        cls.patches = [mock.patch.object(paths, "SESSIONS_DIR", data / "sessions"),
                       mock.patch.object(paths, "SCREENSHOT_DIR", data / "screenshots")]
        for patcher in cls.patches:
            patcher.start()

    @classmethod
    def tearDownClass(cls):
        for patcher in reversed(cls.patches):
            patcher.stop()
        cls.temp.cleanup()

    def setUp(self):
        library.clear_cache()

    def test_same_minute_runs_stay_distinct(self):
        at = AppTest.from_file(str(APP), default_timeout=60)
        at.run()
        at.switch_page("ui/pages/compare.py").run()
        self.assertEqual([item.value for item in at.exception], [])
        widget = at.multiselect(key="w::compare_ids")
        self.assertEqual(sorted(widget.value), sorted(self.ids[:2]))
        labels = [widget.format_func(value) for value in self.ids]
        self.assertEqual(len(set(labels)), 3, labels)
        widget.set_value(self.ids).run()
        self.assertEqual([item.value for item in at.exception], [])
        self.assertEqual(at.multiselect(key="w::compare_ids").value, self.ids)
        overview = next(item.value for item in at.dataframe if "session" in item.value.columns)
        self.assertEqual(list(overview["session"]), ["A", "B", "C"])
        pairs = next(item.value for item in at.dataframe if "pair" in item.value.columns)
        self.assertEqual(list(pairs["pair"]), ["A–B", "A–C", "B–C"])
        self.assertLess(pairs.loc[pairs["pair"] == "A–B", "recommended"].item(), 1.0)


if __name__ == "__main__":
    unittest.main()
