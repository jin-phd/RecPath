"""Browser tests against offline fixture pages (no network). Skipped if Chromium cannot start."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))  # also works as `-m unittest tests.x`
import mweb_fixture  # noqa: E402
from recpath import browser_observation as bo
from recpath.storage import save_session


def chromium_available() -> bool:
    if not bo.playwright_available():
        return False
    bo.ensure_project_browser_path()
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
        return True
    except Exception:
        return False


CHROMIUM = chromium_available()


def fast_settings(**overrides) -> bo.BrowserObservationSettings:
    values = {
        "max_rounds": 3,
        "max_candidates": 4,
        "slow_mo_ms": 0,
        "max_scroll_passes": 1,
        "watch_dwell_ms": 0,
        "exclude_policy": "watched",
    }
    values.update(overrides)
    return bo.BrowserObservationSettings(**values)


def routed_launch(original, handler=mweb_fixture.route_handler):
    def launch(p, settings):
        browser, context, device = original(p, settings)
        context.route("**/*", handler)
        return browser, context, device

    return launch


@unittest.skipUnless(CHROMIUM, "Playwright Chromium is not available")
class FeedExtractorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)
        device = {k: v for k, v in cls.playwright.devices["Pixel 7"].items() if k != "default_browser_type"}
        cls.context = cls.browser.new_context(**device)
        cls.context.route("**/*", mweb_fixture.route_handler)
        cls.page = cls.context.new_page()
        cls.lockup_context = cls.browser.new_context(**device)
        cls.lockup_context.route("**/*", mweb_fixture.make_route_handler(layout="lockup"))
        cls.lockup_page = cls.lockup_context.new_page()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def read(self, url, page_kind):
        self.page.goto(url)
        return bo.read_feed(self.page, page_kind)

    def test_watch_feed_skips_open_app_link_and_reads_every_related_section(self):
        current = "CURRENTvid1"
        feed = self.read(f"https://m.youtube.com/watch?v={current}", "watch")
        self.assertTrue(feed["scope_found"])
        self.assertEqual(feed["section_count"], 3)
        self.assertEqual(feed["current_video_id"], current)
        ids = [item["video_id"] for item in feed["items"]]
        for video_id in mweb_fixture.WATCH_VIDEO_IDS:
            self.assertIn(video_id, ids)
        self.assertNotIn("AAAAAAAAAAA", ids)  # companion ad link
        self.assertEqual(feed["unmatched_links"]["count"], 0)
        self.assertEqual({item["section_index"] for item in feed["items"]}, {0, 2})

        results = bo.normalize_feed_items(
            feed["items"],
            source="watch_recommendations",
            page_kind="watch",
            current_video_id=feed["current_video_id"],
            viewport_height=feed["viewport_height"],
            max_candidates=20,
        )
        self.assertNotIn(current, [item["video_id"] for item in results])
        self.assertEqual([item["raw_position"] for item in results], list(range(1, len(results) + 1)))
        tops = [item["doc_top"] for item in results]
        self.assertEqual(tops, sorted(tops))

    def test_watch_feed_titles_channels_and_labels_come_from_the_card(self):
        feed = self.read("https://m.youtube.com/watch?v=CURRENTvid2", "watch")
        normal = [item for item in feed["items"] if item["label"] == "normal_video"]
        self.assertGreaterEqual(len(normal), 6)
        for item in normal:
            self.assertTrue(item["title"])
            self.assertFalse(item["title"].startswith("Go to channel"))
            self.assertTrue(item["channel_name"])
            self.assertTrue(item["channel_url"].startswith("/@"))
            self.assertIsNotNone(bo.parse_duration_text(item["duration_text"]))
        live = [item for item in feed["items"] if item["label"] == "live"]
        self.assertEqual(len(live), 1)

    def test_radio_start_link_on_an_ordinary_video_is_not_a_mix(self):
        feed = self.read("https://m.youtube.com/watch?v=CURRENTvid3", "watch")
        item = next(i for i in feed["items"] if i["video_id"] == mweb_fixture.RADIO_START_VIDEO_ID)
        self.assertEqual(item["label"], "normal_video")
        self.assertTrue(item["start_radio"])
        self.assertTrue(item["playlist_id"].startswith("RD"))

    def test_missing_feed_section_yields_no_items(self):
        self.page.goto("https://m.youtube.com/results?search_query=cats")
        feed = bo.read_feed(self.page, "watch")  # a search page has no related-items section
        self.assertFalse(feed["scope_found"])
        self.assertEqual(feed["items"], [])

    def test_stale_cards_are_flagged(self):
        self.page.goto("https://m.youtube.com/watch?v=CURRENTvid4")
        self.page.evaluate(bo.FEED_CARDS_JS, {"mode": "mark_stale", "pageKind": "watch"})
        feed = bo.read_feed(self.page, "watch")
        self.assertTrue(feed["items"])
        self.assertTrue(all(item["stale"] for item in feed["items"]))
        self.assertEqual(self.page.evaluate(bo.FEED_CARDS_JS, {"mode": "fresh_count", "pageKind": "watch"}), 0)

    def test_search_feed_labels_shorts_and_ads(self):
        feed = self.read("https://m.youtube.com/results?search_query=cats", "search")
        labels = [item["label"] for item in feed["items"]]
        self.assertIn("short", labels)
        self.assertIn("ad", labels)
        normal_ids = [item["video_id"] for item in feed["items"] if item["label"] == "normal_video"]
        self.assertEqual(normal_ids, mweb_fixture.SEARCH_VIDEO_IDS)
        shorts = [item for item in feed["items"] if item["label"] == "short"]
        self.assertTrue(all(item["shelf"] for item in shorts))
        self.assertTrue(all(item["channel_name"] for item in shorts))

    def read_lockup(self, url, page_kind):
        self.lockup_page.goto(url)
        return bo.read_feed(self.lockup_page, page_kind)

    def test_lockup_search_feed_reads_videos_playlists_and_mixes(self):
        feed = self.read_lockup("https://m.youtube.com/results?search_query=cats", "search")
        self.assertEqual(feed["unmatched_links"]["count"], 0)
        normal = [item for item in feed["items"] if item["label"] == "normal_video"]
        self.assertEqual(
            [item["video_id"] for item in normal],
            mweb_fixture.SEARCH_VIDEO_IDS + [mweb_fixture.LOCKUP_NO_AVATAR_VIDEO_ID],
        )
        for item in normal:
            self.assertEqual(item["card_tag"], "yt-lockup-view-model")
            self.assertEqual(item["title"], f"Lockup video {item['video_id']}")
            self.assertTrue(item["title_aria"].startswith(item["title"]))
            self.assertEqual((item["duration_text"], item["time_style"]), ("12:34", "DEFAULT"))
            self.assertAlmostEqual(item["thumb_aspect"], 0.56, places=2)
        channels = {item["video_id"]: item["channel_name"] for item in normal}
        self.assertEqual(channels[mweb_fixture.LOCKUP_NO_AVATAR_VIDEO_ID], "Rows only channel")  # from the rows
        first = mweb_fixture.SEARCH_VIDEO_IDS[0]
        self.assertEqual(channels[first], f"Lockup channel {first[:4]}")  # from the avatar
        labels = {item["playlist_id"] or item["video_id"]: item["label"] for item in feed["items"]}
        self.assertEqual(labels[mweb_fixture.LOCKUP_PLAYLIST_ID], "playlist")
        self.assertEqual(labels[f"RD{mweb_fixture.LOCKUP_MIX_VIDEO_ID}"], "mix")  # its "Mix" badge is no duration
        self.assertIn("short", labels.values())  # the Shorts shelf is unchanged
        self.assertIn("ad", labels.values())

    def test_lockup_watch_feed_reads_every_related_card(self):
        current = "CURRENTlck1"
        feed = self.read_lockup(f"https://m.youtube.com/watch?v={current}", "watch")
        self.assertEqual(feed["unmatched_links"]["count"], 0)
        ids = [item["video_id"] for item in feed["items"]]
        for video_id in mweb_fixture.WATCH_VIDEO_IDS:
            self.assertIn(video_id, ids)
        self.assertIn(current, ids)  # the playing video's own card, dropped later by normalize_feed_items
        live = [item for item in feed["items"] if item["label"] == "live"]
        self.assertEqual([(item["duration_text"], item["time_style"]) for item in live], [("LIVE", "LIVE")])
        radio = next(i for i in feed["items"] if i["video_id"] == mweb_fixture.RADIO_START_VIDEO_ID)
        self.assertEqual(radio["label"], "normal_video")  # it has a duration, so it is no Mix
        self.assertTrue(radio["start_radio"])

    def test_mark_and_link_find_a_lockup_card(self):
        self.lockup_page.goto("https://m.youtube.com/results?search_query=cats")
        video_id = mweb_fixture.SEARCH_VIDEO_IDS[1]
        args = {"pageKind": "search", "videoId": video_id}
        self.assertTrue(self.lockup_page.evaluate(bo.FEED_CARDS_JS, {"mode": "mark", **args}))
        link = self.lockup_page.evaluate_handle(bo.FEED_CARDS_JS, {"mode": "link", **args}).as_element()
        self.assertIn(video_id, link.get_attribute("href"))

    def test_mark_and_link_find_the_selected_card(self):
        self.page.goto("https://m.youtube.com/results?search_query=cats")
        video_id = mweb_fixture.SEARCH_VIDEO_IDS[2]
        self.assertTrue(self.page.evaluate(bo.FEED_CARDS_JS, {"mode": "mark", "pageKind": "search", "videoId": video_id}))
        self.assertEqual(self.page.locator("[data-recpath-selected]").count(), 1)
        self.page.evaluate(bo.FEED_CARDS_JS, {"mode": "unmark", "pageKind": "search"})
        self.assertEqual(self.page.locator("[data-recpath-selected]").count(), 0)
        link = self.page.evaluate_handle(
            bo.FEED_CARDS_JS, {"mode": "link", "pageKind": "search", "videoId": video_id}
        ).as_element()
        self.assertIn(video_id, link.get_attribute("href"))


@unittest.skipUnless(CHROMIUM, "Playwright Chromium is not available")
class RunnerOfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.serve()

    def serve(self, **options):
        """(Re)route the runner's browser to fixture pages with the given server options."""
        self.route_with(mweb_fixture.make_route_handler(**options))

    def route_with(self, handler):
        if getattr(self, "patcher", None):
            self.patcher.stop()
        self.patcher = mock.patch.object(bo, "launch_browser", routed_launch(self.original_launch, handler))
        self.patcher.start()
        self.addCleanup(lambda: self.patcher.stop() if self.patcher else None)

    original_launch = staticmethod(bo.launch_browser)

    def run_chain(self, settings, checkpoint=None, **kwargs):
        return bo.run_browser_observation(
            start_query="funny cats",
            settings=settings,
            screenshot_dir=self.dir,
            checkpoint=checkpoint or (lambda session: save_session(session, data_dir=self.dir)),
            **kwargs,
        )

    def test_chain_taps_through_rounds_and_records_evidence(self):
        saved_rounds = []
        session = self.run_chain(
            fast_settings(consent_choice="reject_all"),
            checkpoint=lambda s: saved_rounds.append(len(s["rounds"])),
        )
        self.assertEqual(session["status"], "completed", session["error"])
        self.assertEqual(session["end_reason"], "max_rounds")
        self.assertEqual(len(session["rounds"]), 3)
        self.assertEqual(saved_rounds[0], 1)  # saved as soon as round 1 finished

        events = session["consent"]["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["choice"], "reject_all")
        self.assertTrue(events[0]["ok"])
        self.assertTrue(events[0]["clicked_label"].startswith("Reject"))

        first, second, third = session["rounds"]
        self.assertEqual(first["source"], "search_results")
        self.assertEqual(first["navigation"]["method"], "tap")
        self.assertEqual(second["current_video_id"], first["selected_result"]["video_id"])
        self.assertEqual(second["navigation"]["method"], "tap")
        self.assertTrue(second["navigation"]["related_refreshed"])
        self.assertEqual(third["current_video_id"], second["selected_result"]["video_id"])
        self.assertIsNone(third["navigation"])  # no navigation after the last round
        for round_record in session["rounds"]:
            self.assertEqual(round_record["selection"]["selected_display_rank"], 1)
            for kind in ("landing", "selected"):
                self.assertTrue(Path(round_record["screenshots"][kind]).exists(), kind)
            if round_record["current_video_id"]:
                self.assertNotIn(
                    round_record["current_video_id"],
                    [item["video_id"] for item in round_record["results"]],
                )
        self.assertIn("ad", first["label_counts"])

    def test_chain_runs_on_lockup_cards(self):
        self.serve(layout="lockup")
        session = self.run_chain(fast_settings())
        self.assertEqual(session["status"], "completed", session["error"])
        self.assertEqual(len(session["rounds"]), 3)
        for round_record in session["rounds"]:
            normal = [item for item in round_record["results"] if item["label"] == "normal_video"]
            self.assertTrue(normal)
            for item in normal:
                self.assertEqual((item["card_tag"], item["presentation"]), ("yt-lockup-view-model", "video_card"))
                self.assertTrue(item["channel_name"])
                self.assertEqual(item["duration_seconds"], 754)
        self.assertEqual(session["rounds"][1]["navigation"]["method"], "tap")

    def test_failed_navigation_keeps_finished_rounds_and_its_record(self):
        first_pick = mweb_fixture.SEARCH_VIDEO_IDS[0]
        self.serve(abort_video_ids={first_pick})
        session = self.run_chain(fast_settings(navigation="goto"))
        self.assertEqual(session["status"], "error")
        self.assertEqual(session["end_reason"], "error")
        self.assertEqual(len(session["rounds"]), 1)
        self.assertEqual(session["error"]["round"], 1)
        navigation = session["rounds"][0]["navigation"]
        self.assertEqual(navigation["requested_video_id"], first_pick)
        self.assertEqual(navigation["method"], None)
        self.assertIn("ERR_", navigation["error"])
        saved = json.loads((self.dir / f"{session['session_id']}.json").read_text())
        self.assertEqual(saved["status"], "error")
        self.assertEqual(len(saved["rounds"]), 1)

    def test_first_page_dropped_once_is_opened_on_the_retry(self):
        self.route_with(mweb_fixture.make_flaky_handler(1))
        with mock.patch.object(bo, "FIRST_PAGE_RETRY_WAIT_MS", 0):
            session = self.run_chain(fast_settings(max_rounds=1))
        self.assertEqual(session["status"], "completed", session["error"])
        self.assertEqual(len(session["rounds"]), 1)
        self.assertIn("--disable-quic", session["client"]["browser_args"])

    def test_unreachable_youtube_is_reported_as_a_network_problem(self):
        self.route_with(mweb_fixture.make_flaky_handler(bo.FIRST_PAGE_ATTEMPTS))
        with mock.patch.object(bo, "FIRST_PAGE_RETRY_WAIT_MS", 0):
            session = self.run_chain(fast_settings(max_rounds=1))
        self.assertEqual((session["status"], session["end_reason"]), ("error", "error"))
        self.assertEqual(session["rounds"], [])
        message = session["error"]["message"]
        self.assertTrue(message.startswith("NetworkError: Could not reach YouTube (ERR_CONNECTION_RESET"), message)
        self.assertIn("VPN", message)

    def test_failing_saves_do_not_end_the_run(self):
        def broken_checkpoint(session):
            raise OSError("disk full")

        session = self.run_chain(fast_settings(), checkpoint=broken_checkpoint)
        self.assertEqual(session["status"], "completed")
        self.assertEqual(len(session["rounds"]), 3)
        self.assertTrue(session["save_errors"])
        self.assertIn("disk full", session["save_errors"][0]["message"])

    def test_interrupt_is_saved_as_interrupted(self):
        snapshots = []

        def interrupting_checkpoint(session):
            snapshots.append(json.loads(json.dumps(session)))
            if len(snapshots) == 1:
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.run_chain(fast_settings(), checkpoint=interrupting_checkpoint)
        self.assertIsNone(snapshots[0]["end_reason"])  # never claims max_rounds mid-run
        self.assertEqual(snapshots[-1]["status"], "interrupted")
        self.assertEqual(snapshots[-1]["end_reason"], "interrupted")

    def test_continuation_from_a_watch_page_survives_a_reloading_cookie_dialog(self):
        self.serve(consent="reload")
        start_id = mweb_fixture.SEARCH_VIDEO_IDS[1]
        session = self.run_chain(
            fast_settings(max_rounds=2),
            start_url=f"https://m.youtube.com/watch?v={start_id}",
            initial_seen_ids=["inheritedX1"],
        )
        self.assertEqual(session["status"], "completed", session["error"])
        self.assertEqual(len(session["rounds"]), 2)
        self.assertEqual(session["rounds"][0]["current_video_id"], start_id)
        self.assertTrue(session["consent"]["applied"])
        self.assertIn(start_id, session["initial_excluded_video_ids"])
        self.assertIn("inheritedX1", session["initial_excluded_video_ids"])

    def test_consent_that_cannot_be_answered_as_requested_stops_the_run(self):
        self.serve(consent="accept_only")
        session = self.run_chain(fast_settings(consent_choice="reject_all"))
        self.assertEqual(session["status"], "error")
        self.assertEqual(session["rounds"], [])
        self.assertIn("reject_all", session["error"]["message"])
        # The failed attempt itself is part of the record.
        self.assertTrue(session["consent"]["checked"])
        self.assertFalse(session["consent"]["applied"])
        self.assertEqual(len(session["consent"]["events"]), 1)
        self.assertFalse(session["consent"]["events"][0]["ok"])

    def test_content_location_holds_on_every_round(self):
        session = self.run_chain(fast_settings(region="GB"))
        self.assertEqual(session["status"], "completed", session["error"])
        self.assertEqual((session["settings"]["region"], session["settings"]["location_cookie"]), ("GB", True))
        for round_record in session["rounds"]:
            self.assertEqual(
                round_record["location"],
                {"requested": "GB", "gl": "GB", "hl": "en", "pref_gl": "GB", "ok": True},
            )
        self.assertEqual(session["client"]["innertube_client"]["gl"], "GB")
        self.assertEqual(session["client"]["locale"], "en-IE")  # only the content location changes

    def test_content_location_youtube_did_not_apply_ends_the_run(self):
        # Without the PREF cookie the gl in the search URL is lost and the IP's location applies.
        session = self.run_chain(fast_settings(region="US", location_cookie=False))
        self.assertEqual(session["status"], "error")
        self.assertEqual(session["end_reason"], "location_mismatch")
        self.assertEqual(session["rounds"], [])
        self.assertIn("'IE'", session["error"]["message"])

    def test_page_without_client_config_records_an_unverified_location(self):
        self.serve(ip_location=None)
        session = self.run_chain(fast_settings(max_rounds=1, region="GB"))
        self.assertEqual(session["status"], "completed", session["error"])
        self.assertEqual(session["rounds"][0]["location"]["gl"], None)
        self.assertIsNone(session["rounds"][0]["location"]["ok"])

    def test_cookie_dialog_that_ignores_the_first_click_is_answered_on_the_retry(self):
        self.serve(consent="late")
        session = self.run_chain(fast_settings(max_rounds=1))
        self.assertEqual(session["status"], "completed", session["error"])
        event = session["consent"]["events"][0]
        self.assertTrue(event["ok"])
        self.assertTrue(event["retried"])

    def test_watch_round_records_player_ad_schedule_and_reel_signal(self):
        self.serve(player_ad=True, page_data=True)
        session = self.run_chain(fast_settings(max_rounds=2, max_candidates=10))
        self.assertEqual(session["status"], "completed", session["error"])
        search_round, watch_round = session["rounds"]
        self.assertIsNone(search_round["ads"]["player"])
        self.assertEqual(search_round["ads"]["feed_ads"], 1)

        ads = watch_round["ads"]
        self.assertTrue(ads["player"]["ad_seen"])
        self.assertEqual(len(ads["player"]["ads"]), 1)
        ad = ads["player"]["ads"][0]
        self.assertEqual(ad["title"], "Fixture ad title")
        self.assertEqual(ad["pod_position"], "1 of 2")
        self.assertTrue(ad["skippable"])
        self.assertTrue(Path(watch_round["screenshots"]["ad"]).exists())
        self.assertEqual(ads["companion"]["headline"], "Fixture companion headline")
        self.assertEqual(ads["companion"]["cta_host"], "shop.example")
        kinds = [(slot["kind"], slot["offset_ms"]) for slot in ads["scheduled"]]
        self.assertIn(("preroll", None), kinds)
        self.assertIn(("midroll", 90000), kinds)
        preroll = next(slot for slot in ads["scheduled"] if slot["kind"] == "preroll")
        self.assertEqual(preroll["ad_video_ids"], [mweb_fixture.SCHEDULED_AD_VIDEO_ID])
        self.assertEqual(preroll["texts"], ["Fixture headline"])

        reel = next(item for item in watch_round["raw_results"] if item["video_id"] == mweb_fixture.REEL_VIDEO_ID)
        self.assertEqual(reel["label"], "short")
        self.assertIn("innertube:reel_endpoint", reel["shorts_evidence"])
        self.assertEqual(reel["channel_id"], "UCreel")
        self.assertEqual(reel["presentation"], "video_card")

    def test_shorts_feed_records_each_short_and_the_ad(self):
        events = []
        session = bo.run_shorts_feed(
            start_query="funny cats",
            settings=fast_settings(),
            items=3,
            screenshot_dir=self.dir,
            checkpoint=lambda s: save_session(s, data_dir=self.dir),
            on_event=lambda e: events.append(e["type"]),
        )
        self.assertEqual(session["status"], "completed", session["error"])
        self.assertEqual(session["mode"], "shorts_feed")
        feed = session["shorts_feed"]
        self.assertEqual([item["video_id"] for item in feed], mweb_fixture.SHORTS_SEQUENCE[:3])
        self.assertEqual([item["is_ad"] for item in feed], [False, True, False])
        self.assertEqual(feed[0]["channel_name"], "@channel_0")
        self.assertTrue(feed[0]["title"].startswith("Short title"))
        self.assertEqual(feed[1]["ad"]["cta_host"], "shop.example")
        self.assertIn("Fixture shorts ad headline", feed[1]["ad"]["headline"])
        for item in feed:
            self.assertTrue(Path(item["screenshot"]).exists())
        self.assertEqual(events.count("short"), 3)
        self.assertTrue(session["shorts_start"]["location"]["ok"])
        # Chain-only settings are not recorded for a Shorts feed.
        self.assertNotIn("navigation", session["settings"])
        self.assertFalse(session["settings"]["youtube_api_enrichment"])
        saved = json.loads((self.dir / f"{session['session_id']}.json").read_text())
        self.assertEqual(len(saved["shorts_feed"]), 3)

    def test_stop_during_the_last_round_is_recorded(self):
        stop = {"now": False}

        def on_event(event):
            if event["type"] == "round" and event["round"] == 2:
                stop["now"] = True

        session = self.run_chain(fast_settings(max_rounds=2), on_event=on_event, should_stop=lambda: stop["now"])
        self.assertEqual(session["status"], "completed", session["error"])
        self.assertEqual(len(session["rounds"]), 2)
        self.assertEqual(session["end_reason"], "stopped")
        rows = (self.dir / f"{session['session_id']}.csv").read_text().splitlines()
        self.assertIn("session_end_reason", rows[0])
        self.assertIn("stopped", rows[1])

    def test_unreachable_start_page_is_reported_not_raised(self):
        session = self.run_chain(fast_settings(), start_url="https://m.youtube.com/unknown-path")
        self.assertEqual(session["status"], "error")
        self.assertEqual(session["rounds"], [])

    def test_displayed_policy_excludes_everything_already_shown(self):
        session = self.run_chain(fast_settings(exclude_policy="displayed", max_candidates=10))
        shown = set()
        for round_record in session["rounds"]:
            selected = (round_record["selected_result"] or {}).get("video_id")
            if selected:
                self.assertNotIn(selected, shown)
            shown.update(item["video_id"] for item in round_record["raw_results"] if item["video_id"])
        self.assertIn(session["end_reason"], {"max_rounds", "no_candidates"})


if __name__ == "__main__":
    unittest.main()
