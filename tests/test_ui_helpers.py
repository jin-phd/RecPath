"""The pure helpers behind the pages: wording, escaping, candidate columns, ads rows, round facts."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # also works as `-m unittest tests.x`
import session_fixtures  # noqa: E402
from ui import common, components  # noqa: E402
from recpath import library  # noqa: E402


class WordingTests(unittest.TestCase):
    def test_end_reasons_in_words(self):
        self.assertEqual(common.end_reason_text("feed_stuck", "shorts_feed"), "the Shorts feed stopped advancing")
        self.assertEqual(common.end_reason_text("max_rounds", "shorts_feed"), "all requested Shorts recorded")
        self.assertEqual(common.end_reason_text("max_rounds", "browser_observation"), "all planned rounds finished")
        self.assertEqual(common.end_reason_text("something_new"), "something new")
        self.assertEqual(common.end_reason_text(None), "")

    def test_error_text_is_escaped_and_ends_without_a_full_stop(self):
        text = common.sentence("Target closed [details]_x_ *a* <b>. ")
        self.assertNotIn("_x_", text)
        self.assertIn(r"\_x\_", text)
        self.assertIn(r"\[details\]", text)
        self.assertFalse(text.endswith("."))
        self.assertEqual(common.sentence("Timeout 12000ms exceeded.") + ".", r"Timeout 12000ms exceeded.")
        self.assertEqual(common.sentence(None), "unknown error")

    def test_error_place(self):
        self.assertEqual(common.error_place(2, 1), "in round 2")  # round 2 was being read
        self.assertEqual(common.error_place(1, 1), "after round 1")  # round 1 was saved: opening the next video
        self.assertEqual(common.error_place(3, 2, "Short", leading=" "), " in Short 3")
        self.assertEqual(common.error_place(None, 0), "")

    def test_default_rounds_is_clamped(self):
        class Config:
            max_rounds = 12

        self.assertEqual(common.default_rounds(Config()), 10)
        Config.max_rounds = 0
        self.assertEqual(common.default_rounds(Config()), 1)
        Config.max_rounds = 4
        self.assertEqual(common.default_rounds(Config()), 4)


class CandidateTests(unittest.TestCase):
    def phase2_session(self):
        session = session_fixtures.v3_ads_session()
        first = session["rounds"][0]["raw_results"][0]
        first.update(shorts_evidence=["shorts_url:redirect"], duration_bucket="61-180s",
                     innertube={"renderer": "videoWithContextRenderer", "endpoint": "watch"})
        return session

    def test_phase2_item_fields_become_columns_when_present(self):
        frame = components.candidates_frame(self.phase2_session())
        columns = components.candidate_columns(frame)
        for name in ("shorts_evidence", "duration_bucket", "renderer", "presentation"):
            self.assertIn(name, columns)
            self.assertIn(name, components.CANDIDATE_COLUMN_CONFIG)
        self.assertEqual(frame.loc[0, "shorts_evidence"], ["shorts_url:redirect"])
        legacy = components.candidates_frame(session_fixtures.legacy_browser_session())
        legacy_columns = components.candidate_columns(legacy)
        for name in ("shorts_evidence", "duration_bucket", "renderer", "presentation"):
            self.assertNotIn(name, legacy_columns)

    def test_compact_columns_put_the_title_first(self):
        frame = components.candidates_frame(self.phase2_session(), [1])
        columns = components.candidate_columns(frame, with_round=False, compact=True)
        self.assertEqual(columns[:3], ["picked", "title", "type"])
        self.assertLess(columns.index("title"), columns.index("thumb"))


class AdsTests(unittest.TestCase):
    def test_ads_rows_carry_beacons_and_scheduled_ad_videos(self):
        session = session_fixtures.v3_ads_session()
        ads = session["rounds"][1]["ads"]
        ads["beacons"] = {"stats_ads": 2, "pagead_adview": 1, "activeview": 0}
        ads["scheduled"] = [{"kind": "preroll", "offset_ms": 0, "ad_video_ids": ["adVid1", "adVid2"], "texts": ["Try X"]}]
        rows = components.ads_rows(session)
        second = rows[1]
        self.assertEqual(second["beacons"], 3)
        self.assertEqual(second["beacon_detail"], "stats_ads 2 · pagead_adview 1")
        self.assertEqual(second["scheduled_ad_ids"], "adVid1, adVid2")
        self.assertEqual(second["scheduled_texts"], "Try X")
        self.assertIsNone(rows[0]["beacons"])  # no beacon record in that round
        for column in second:
            self.assertIn(column, components.ADS_COLUMN_CONFIG)

    def test_ad_tile_names_what_it_counts(self):
        label, value, help_text = components.ads_tile(0, library.ADS_SCOPE_FEED_ONLY)
        self.assertEqual((label, value), ("Feed ads", 0))
        self.assertIn("unknown, not zero", help_text)
        self.assertEqual(components.ads_tile(2, library.ADS_SCOPE_FEED_AND_PLAYER)[0], "Ads seen")


class RoundFactsTests(unittest.TestCase):
    def test_facts_of_a_v2_round(self):
        record = session_fixtures.v2_session(Path("/nonexistent"))["rounds"][1]
        record.update(feed_scope_found=True, stale_card_count=0,
                      scroll={"passes": 3, "stalled": False, "cap_reached": True})
        record["navigation"].update(elapsed_ms=1234, fallback_reason="tap timed out")
        facts = {row["fact"]: row["value"] for row in components.round_facts(record)}
        self.assertEqual(facts["Source"], "up next")
        self.assertEqual(facts["Cards: observed / selectable / already seen"], "3 / 3 / 0")
        self.assertEqual(facts["Recommendation list found"], "yes")
        self.assertEqual(facts["Scrolling"], "3 passes · stalled no · cap reached yes")
        self.assertEqual(facts["Navigation time"], "1.2 s")
        self.assertEqual(facts["Tap fallback reason"], "tap timed out")
        stripped = components.round_record_without_items(record)
        self.assertNotIn("raw_results", stripped)
        self.assertNotIn("results", stripped)
        self.assertIn("navigation", stripped)

    def test_facts_of_a_legacy_round_list_only_what_it_has(self):
        record = session_fixtures.legacy_browser_session()["rounds"][0]
        facts = [row["fact"] for row in components.round_facts(record)]
        self.assertIn("Page URL", facts)
        self.assertNotIn("Recommendation list found", facts)
        self.assertNotIn("Next video opened by", facts)


if __name__ == "__main__":
    unittest.main()


class FinalReviewFixTests(unittest.TestCase):
    def test_early_endings_are_not_reported_as_plain_success(self):
        self.assertEqual(common.early_end_alert("feed_not_found"), "warning")
        self.assertEqual(common.early_end_alert("no_candidates"), "info")
        self.assertIsNone(common.early_end_alert("max_rounds"))
        self.assertIsNone(common.early_end_alert(None))

    def test_view_counts_from_page_text(self):
        parse = components.parse_view_count_text
        self.assertEqual([parse(t) for t in ("279K views", "1.2M views", "1,234 views", "No views")],
                         [279_000, 1_200_000, 1234, 0])
        self.assertIsNone(parse(None))

    def test_search_rounds_do_not_count_as_player_observed(self):
        search = {"round": 1, "source": "search_results", "label_counts": {"ad": 1},
                  "ads": {"player": None, "observed_ms": 0, "feed_ads": 1}}
        watch = {"round": 2, "source": "watch_recommendations", "label_counts": {},
                 "ads": {"player": {"ad_seen": False, "ads": []}, "observed_ms": 6000, "feed_ads": 0}}
        self.assertFalse(library.player_observed(search))
        self.assertTrue(library.player_observed(watch))
        self.assertEqual(library.ads_scope({"mode": "browser_observation", "rounds": [search]}),
                         library.ADS_SCOPE_FEED_ONLY)
        self.assertEqual(library.ads_scope({"mode": "browser_observation", "rounds": [search, watch]}),
                         library.ADS_SCOPE_FEED_AND_PLAYER)
        self.assertIsNone(components.ads_rows({"rounds": [search]})[0]["player_ad"])

    def test_one_ad_recorded_twice_by_older_runs_counts_once(self):
        blank = {"title": None, "pod_text": None, "pod_position": None, "advertiser_host": None}
        record = {"round": 2, "label_counts": {}, "ads": {"player": {"ad_seen": True, "ads": [
            blank, {**blank, "pod_text": "Sponsored 1 of 2", "pod_position": "1 of 2"}]}}}
        self.assertEqual(library.round_ad_count(record), 1)
