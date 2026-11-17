import threading
import time
import unittest
from unittest import mock

from recpath import api_tracking, browser_observation, library, run_manager
from recpath.browser_observation import BrowserObservationSettings


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class FakeRunner:
    """Stands in for run_browser_observation: emits events and frames, then waits to be released."""

    def __init__(self, *, rounds=2, block=True, fail=None, status="completed"):
        self.rounds = rounds
        self.release = threading.Event()
        if not block:
            self.release.set()
        self.fail = fail
        self.status = status
        self.calls = []
        self.started = threading.Event()

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        on_event, on_frame = kwargs["on_event"], kwargs["on_frame"]
        should_stop, checkpoint = kwargs["should_stop"], kwargs["checkpoint"]
        session = {"session_id": "session_fake_1", "status": "running", "end_reason": None, "error": None, "rounds": []}
        on_event({"type": "started", "session_id": session["session_id"], "at": "2026-10-20T10:00:00Z",
                  "start_query": kwargs.get("start_query")})
        self.started.set()
        if self.fail:
            raise self.fail
        for number in range(1, self.rounds + 1):
            if should_stop():
                session["end_reason"] = "stopped"
                break
            record = {"round": number, "selected_result": {"video_id": f"v{number}", "title": f"Video {number}"}}
            on_event({"type": "step", "session_id": session["session_id"], "round": number, "step": "scrolling",
                      "screenshot": f"data/screenshots/fake_{number}.jpg"})
            on_frame(b"\xff\xd8frame%d" % number)
            session["rounds"].append(record)
            on_event({"type": "round", "session_id": session["session_id"], "round": number, "record": record})
            record["navigation"] = {"method": "tap"}  # mutated after the event, like the real runner
            checkpoint(session)
            self.release.wait(5)
        session["status"] = self.status
        if self.status == "error":
            session["error"] = {"round": 1, "message": "BrowserObservationError: feed vanished"}
            session["end_reason"] = "error"
        session["end_reason"] = session["end_reason"] or "max_rounds"
        on_event({"type": "finished", "session_id": session["session_id"], "status": session["status"],
                  "end_reason": session["end_reason"], "error": session["error"]})
        return session


class RunManagerTests(unittest.TestCase):
    def setUp(self):
        run_manager._reset_for_tests()
        self.addCleanup(run_manager._reset_for_tests)
        self.saved = []

    def checkpoint(self, session):
        self.saved.append(len(session["rounds"]))
        return ("/tmp/x.json", "/tmp/x.csv")

    def start(self, runner, **kwargs):
        patcher = mock.patch.object(browser_observation, "run_browser_observation", runner)
        patcher.start()
        self.addCleanup(patcher.stop)
        return run_manager.start_chain(
            start_query="funny cats",
            settings=BrowserObservationSettings(max_rounds=runner.rounds),
            checkpoint=self.checkpoint,
            **kwargs,
        )

    def test_only_one_run_at_a_time(self):
        runner = FakeRunner()
        handle = self.start(runner)
        self.assertTrue(runner.started.wait(5))
        self.assertIs(run_manager.current(), handle)
        with self.assertRaises(run_manager.RunAlreadyActive) as caught:
            run_manager.start_chain(start_query="other", settings=BrowserObservationSettings())
        self.assertIn("funny cats", str(caught.exception))
        self.assertEqual(len(runner.calls), 1)
        runner.release.set()
        self.assertTrue(handle.join(5))
        self.assertIsNone(run_manager.current())
        self.assertIs(run_manager.latest(), handle)
        second = run_manager.start_chain(start_query="again", settings=BrowserObservationSettings(), checkpoint=self.checkpoint)
        self.assertTrue(second.join(5))
        self.assertEqual([item.params["start_query"] for item in run_manager.recent()], ["again", "funny cats"])

    def test_events_frames_and_checkpoints_are_collected(self):
        runner = FakeRunner(block=False)
        handle = self.start(runner)
        self.assertTrue(handle.join(5))
        snap = handle.snapshot()
        self.assertEqual([event["type"] for event in snap["events"]],
                         ["started", "step", "round", "step", "round", "finished"])
        self.assertEqual(snap["session_id"], "session_fake_1")
        self.assertEqual(snap["frame"], b"\xff\xd8frame2")
        self.assertIsNotNone(snap["frame_at"])
        self.assertEqual(snap["frame_count"], 2)
        self.assertEqual(snap["checkpoints"], 2)
        self.assertEqual(self.saved, [1, 2])
        self.assertEqual(snap["saved_paths"], ["/tmp/x.json", "/tmp/x.csv"])
        self.assertEqual(snap["outcome"], "completed")
        self.assertIsNone(snap["failure_message"])
        # The round record was copied when the event arrived, before the runner changed it.
        round_event = next(event for event in snap["events"] if event["type"] == "round")
        self.assertNotIn("navigation", round_event["record"])
        # Hooks were wired by the manager, and the public params never carry a key.
        call = runner.calls[0]
        self.assertTrue(callable(call["should_stop"]) and callable(call["on_frame"]))
        self.assertNotIn("youtube_api_key", handle.params["settings"])

    def test_stop_request_ends_the_run_after_the_current_step(self):
        runner = FakeRunner(rounds=5)
        handle = self.start(runner)
        self.assertTrue(wait_until(lambda: len(self.saved) == 1))
        self.assertTrue(run_manager.request_stop())
        self.assertEqual(handle.outcome(), "stopping")
        runner.release.set()
        self.assertTrue(handle.join(5))
        self.assertEqual(handle.outcome(), "stopped")
        self.assertLess(len(handle.session["rounds"]), 5)
        self.assertFalse(run_manager.request_stop())  # nothing is running any more

    def test_runner_exception_becomes_a_redacted_failure_message(self):
        runner = FakeRunner(fail=RuntimeError("launch failed key=AIzaSyA1234567890abcdefghijklmnop"))
        handle = self.start(runner)
        self.assertTrue(handle.join(5))
        self.assertEqual(handle.outcome(), "error")
        message = handle.failure_message()
        self.assertTrue(message.startswith("RuntimeError: launch failed"))
        self.assertNotIn("AIzaSy", message)
        self.assertIsNone(run_manager.current())  # the slot is released after a crash

    def test_crash_after_a_checkpoint_saves_an_error_status(self):
        saved = []

        def checkpoint(session):
            saved.append({"status": session["status"], "rounds": len(session["rounds"]), "error": session.get("error")})
            return ("/tmp/crash.json", "/tmp/crash.csv")

        def crashing_runner(**kwargs):
            session = {"session_id": "session_crash", "status": "running", "end_reason": None, "error": None, "rounds": []}
            kwargs["on_event"]({"type": "step", "session_id": "session_crash", "round": 1, "step": "scrolling"})
            session["rounds"].append({"round": 1, "selected_result": {"video_id": "a"}})
            kwargs["checkpoint"](session)
            kwargs["on_event"]({"type": "step", "session_id": "session_crash", "round": 2, "step": "navigating"})
            raise RuntimeError("Target page has been closed [details]_x_ key=AIzaSyA1234567890abcdefghijklmnop")

        with mock.patch.object(browser_observation, "run_browser_observation", crashing_runner):
            handle = run_manager.start_chain(start_query="q", settings=BrowserObservationSettings(max_rounds=3),
                                             checkpoint=checkpoint)
            self.assertTrue(handle.join(5))
        self.assertEqual(handle.outcome(), "error")
        self.assertEqual([item["status"] for item in saved], ["running", "error"])
        session = handle.session
        self.assertIsNotNone(session, "the rounds saved before the crash are kept for the outcome view")
        self.assertEqual((session["status"], session["end_reason"]), ("error", "error"))
        self.assertEqual(session["error"]["round"], 2)  # the round of the last progress step
        self.assertIn("Target page has been closed", session["error"]["message"])
        self.assertNotIn("AIzaSy", session["error"]["message"])
        self.assertTrue(session["finished_at"])
        self.assertEqual(len(session["rounds"]), 1)
        snap = handle.snapshot()
        self.assertEqual(snap["checkpoints"], 2)
        self.assertEqual(snap["saved_paths"], ["/tmp/crash.json", "/tmp/crash.csv"])
        self.assertIsNone(run_manager.current())

    def test_crash_before_any_checkpoint_has_no_session(self):
        handle = self.start(FakeRunner(fail=RuntimeError("launch failed")))
        self.assertTrue(handle.join(5))
        self.assertIsNone(handle.session)
        self.assertEqual(self.saved, [])

    def test_crash_whose_final_save_fails_still_reports_the_error(self):
        calls = []

        def checkpoint(session):
            calls.append(session["status"])
            if session["status"] == "error":
                raise OSError("disk full")

        def crashing_runner(**kwargs):
            session = {"session_id": "s", "status": "running", "rounds": [{"round": 1}]}
            kwargs["checkpoint"](session)
            raise RuntimeError("boom")

        with mock.patch.object(browser_observation, "run_browser_observation", crashing_runner):
            handle = run_manager.start_chain(start_query="q", settings=BrowserObservationSettings(), checkpoint=checkpoint)
            self.assertTrue(handle.join(5))
        self.assertEqual(calls, ["running", "error"])
        self.assertEqual(handle.outcome(), "error")
        self.assertFalse(handle.session["last_save_ok"])
        self.assertIn("disk full", handle.session["save_errors"][-1]["message"])

    def test_session_error_status_is_reported(self):
        handle = self.start(FakeRunner(block=False, status="error"))
        self.assertTrue(handle.join(5))
        self.assertEqual(handle.outcome(), "error")
        self.assertIn("feed vanished", handle.failure_message())

    def test_shorts_feed_is_feature_detected(self):
        if not hasattr(browser_observation, "run_shorts_feed"):
            self.assertFalse(run_manager.shorts_feed_available())
            with self.assertRaises(run_manager.RunManagerError):
                run_manager.start_shorts_feed(start_query="x", settings=BrowserObservationSettings())
        runner = FakeRunner(block=False)
        with mock.patch.object(browser_observation, "run_shorts_feed", runner, create=True):
            self.assertTrue(run_manager.shorts_feed_available())
            handle = run_manager.start_shorts_feed(
                start_query="irish shorts", settings=BrowserObservationSettings(), items=7, checkpoint=self.checkpoint
            )
            self.assertTrue(handle.join(5))
        self.assertEqual(handle.kind, run_manager.KIND_SHORTS)
        self.assertEqual(handle.params["items"], 7)
        self.assertEqual(runner.calls[0]["items"], 7)

    def test_api_chain_runs_in_the_background(self):
        class FakeClient:
            def search_videos(self, query, max_results=25):
                ids = [f"{query[:3]}{index}" for index in range(3)]
                results = [
                    {"position": index + 1, "label": "normal_video", "video_id": video_id, "title": f"T {video_id}"}
                    for index, video_id in enumerate(ids)
                ]
                return {"results": results, "raw_search_response": {}, "raw_videos_response": {}}

        handle = run_manager.start_api_chain(
            youtube_api_key="k", entry_value="irish gaming", max_rounds=2, max_results=5,
            client=FakeClient(), checkpoint=self.checkpoint,
        )
        self.assertTrue(handle.join(5))
        session = handle.session
        self.assertEqual(session["mode"], "api_metadata")
        self.assertEqual(session["status"], "completed")
        self.assertEqual(session["start_query"], "irish gaming")
        self.assertEqual(len(session["rounds"]), 2)
        self.assertEqual(session["rounds"][1]["entry_type"], "video")
        self.assertIn("round", [event["type"] for event in handle.events()])
        # A new API session is current-schema, not shown as "older format".
        self.assertFalse(library.is_legacy(session))
        self.assertEqual(session["settings"]["max_rounds"], 2)

    def test_api_chain_error_keeps_rounds(self):
        class FailingClient:
            calls = 0

            def search_videos(self, query, max_results=25):
                FailingClient.calls += 1
                if FailingClient.calls > 1:
                    raise RuntimeError("quota exceeded")
                return {"results": [{"position": 1, "label": "normal_video", "video_id": "a1", "title": "A"}],
                        "raw_search_response": {}, "raw_videos_response": {}}

        session = api_tracking.run_api_chain(youtube_api_key="k", entry_value="q", max_rounds=3, client=FailingClient())
        self.assertEqual(session["status"], "error")
        self.assertEqual(len(session["rounds"]), 1)
        self.assertIn("quota exceeded", session["error"]["message"])


class SettingsHelperTests(unittest.TestCase):
    def test_build_settings_drops_fields_this_runner_lacks(self):
        settings = run_manager.build_settings(strategy="random", max_rounds=3, not_a_field=True, device=None)
        self.assertEqual(settings.strategy, "random")
        self.assertEqual(settings.max_rounds, 3)
        self.assertEqual(settings.device, BrowserObservationSettings().device)  # None means default

    def test_continuation_inherits_the_parent_measurement_settings(self):
        session = {
            "session_id": "session_parent",
            "start_query": "funny cats",
            "settings": {"device": "iPhone 13", "consent_choice": "reject_all", "navigation": "goto",
                         "exclude_policy": "watched", "max_candidates": 10},
            "initial_excluded_video_ids": ["old1"],
            "rounds": [
                {"round": 1, "raw_results": [{"video_id": "a"}, {"video_id": "b"}], "selected_result": {"video_id": "a"}},
                {"round": 2, "raw_results": [{"video_id": "c"}], "selected_result": {"video_id": "c"}},
            ],
        }
        start = {"video_id": "c", "title": "C", "url": "https://m.youtube.com/watch?v=c"}
        kwargs = run_manager.continuation_kwargs(
            session, start_result=start, parent_round=2, strategy="top_1", max_rounds=4,
            options={"device": "Pixel 7", "headless": False, "slow_mo_ms": 100, "youtube_api_key": None},
        )
        settings = kwargs["settings"]
        self.assertEqual((settings.device, settings.consent_choice, settings.navigation, settings.exclude_policy),
                         ("iPhone 13", "reject_all", "goto", "watched"))
        self.assertEqual(settings.max_candidates, 10)
        self.assertEqual(settings.max_rounds, 4)
        self.assertFalse(settings.headless)
        self.assertEqual(settings.slow_mo_ms, 100)
        self.assertEqual(kwargs["start_url"], start["url"])
        self.assertEqual(
            kwargs["parent"],
            {"session_id": "session_parent", "round": 2, "video_id": "c", "title": "C", "kind": "continuation"},
        )
        # "watched": only earlier picks are inherited, not round 2's own pick.
        self.assertEqual(kwargs["initial_seen_ids"], ["a", "old1"])

    def test_continuation_of_a_legacy_session_falls_back_to_options(self):
        kwargs = run_manager.continuation_kwargs(
            {"session_id": "s", "rounds": []}, start_result={"video_id": "x", "title": "X"}, parent_round=1,
            strategy="random", max_rounds=2, options={"device": "iPhone 13", "consent_choice": "reject_all"},
        )
        self.assertEqual(kwargs["settings"].device, "iPhone 13")
        self.assertEqual(kwargs["settings"].consent_choice, "reject_all")
        self.assertEqual(kwargs["start_query"], "X")
        self.assertEqual(kwargs["start_url"], None)

    def test_continuation_keeps_the_parent_content_location(self):
        parent = {"session_id": "s", "mode": "browser_observation", "start_query": "q", "region": "GB",
                  "settings": {"device": "Pixel 7", "region": "GB", "location_cookie": True},
                  "rounds": [{"round": 1, "results": [{"video_id": "a"}], "selected_result": {"video_id": "a"}}]}
        kwargs = run_manager.continuation_kwargs(
            parent, start_result={"video_id": "a", "title": "A"}, parent_round=1, strategy="top_1", max_rounds=2,
            options={"region": "US"}, region="IE",
        )
        self.assertEqual((kwargs["settings"].region, kwargs["settings"].location_cookie), ("GB", True))
        # A session from before the choice: the region at its top level, and no PREF cookie.
        older = dict(parent, settings={"device": "Pixel 7"})
        kwargs = run_manager.continuation_kwargs(
            older, start_result={"video_id": "a", "title": "A"}, parent_round=1, strategy="top_1", max_rounds=2,
            options={"region": "US"}, region="IE",
        )
        self.assertEqual((kwargs["settings"].region, kwargs["settings"].location_cookie), ("GB", False))

    def test_continuation_of_a_legacy_chain_keeps_its_device(self):
        # Before the device was recorded, every chain ran as iPhone 13 (library.device_of).
        legacy = {"session_id": "s", "mode": "browser_observation", "start_query": "q",
                  "rounds": [{"round": 1, "results": [{"video_id": "a"}], "selected_result": {"video_id": "a"}}]}
        # Legacy chains always opened videos by URL, excluded everything displayed and never
        # set the content location cookie.
        self.assertEqual(
            run_manager.inherited_settings(legacy),
            {"device": "iPhone 13", "navigation": "goto", "exclude_policy": "displayed", "location_cookie": False},
        )
        kwargs = run_manager.continuation_kwargs(
            legacy, start_result={"video_id": "a", "title": "A"}, parent_round=1, strategy="top_1", max_rounds=2,
            options={"device": "Pixel 7", "consent_choice": "reject_all", "navigation": "goto"},
        )
        settings = kwargs["settings"]
        self.assertEqual(settings.device, "iPhone 13")
        self.assertEqual((settings.consent_choice, settings.navigation), ("reject_all", "goto"))  # not recorded
        recorded_client = dict(legacy, client={"device": "Pixel 7"})
        self.assertEqual(
            run_manager.inherited_settings(recorded_client),
            {"device": "Pixel 7", "navigation": "goto", "exclude_policy": "displayed", "location_cookie": False},
        )


if __name__ == "__main__":
    unittest.main()
