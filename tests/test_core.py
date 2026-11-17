import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from recpath.browser_observation import (
    count_normal_candidates,
    enrich_browser_results,
    excluded_ids_for_continuation,
    normalize_feed_items,
    open_first_page,
    parse_duration_text,
    search_url,
    video_id_from_url,
)
from recpath.classification import classify_result, parse_iso8601_duration
from recpath.config import parse_runtime_config
from recpath.openai_queries import QueryGenerationError, normalize_query_payload
from recpath.prompts import build_query_user_message
from recpath import safety
from recpath.safety import error_summary, redact_secrets, register_secrets
from recpath.selection import (
    assign_display_ranks,
    filter_excluded_videos,
    select_next_video,
    top_normal_videos,
)
from recpath.storage import flatten_session_rows, save_session
from recpath.youtube import YouTubeApiError, YouTubeClient
from recpath.trends import (
    build_google_trends_explore_url,
    normalize_rss_items,
    normalize_trend_rows,
    parse_google_trends_csv,
    parse_google_trends_rss,
)


class CoreBehaviorTests(unittest.TestCase):
    def test_parse_iso8601_duration(self):
        self.assertEqual(parse_iso8601_duration("PT1H2M3S"), 3723)
        self.assertEqual(parse_iso8601_duration("PT59S"), 59)
        self.assertIsNone(parse_iso8601_duration(None))

    def test_classify_short_by_duration(self):
        label, confidence, _ = classify_result(
            {"raw_type": "video", "duration_seconds": 45}
        )
        self.assertEqual(label, "short")
        self.assertGreater(confidence, 0.8)

    def test_classify_normal_video(self):
        label, _, _ = classify_result({"raw_type": "video", "duration_seconds": 120})
        self.assertEqual(label, "normal_video")

    def test_random_selection_is_reproducible(self):
        candidates = [
            {"video_id": "a", "raw_position": 1, "title": "A"},
            {"video_id": "b", "raw_position": 2, "title": "B"},
        ]
        first, first_record = select_next_video(candidates, "random", seed=7)
        second, second_record = select_next_video(candidates, "random", seed=7)
        self.assertEqual(first, second)
        self.assertEqual(first_record["random_seed"], second_record["random_seed"])
        self.assertEqual(first_record["selected_raw_position"], second_record["selected_raw_position"])

    def test_normalize_query_payload_requires_five(self):
        payload = {
            "queries": [
                {"rank": 1, "query": "irish junior cert study vlog", "source_terms": ["junior cert"]},
                {"rank": 2, "query": "dublin teen streetwear haul"},
                {"rank": 3, "query": "cork gaa training vlog"},
                {"rank": 4, "query": "irish leaving cert reaction"},
                {"rank": 5, "query": "galway summer festival vlog"},
            ]
        }
        normalized = normalize_query_payload(payload)
        self.assertEqual(len(normalized), 5)
        self.assertEqual(normalized[0]["source_terms"], ["junior cert"])

    def test_normalize_query_payload_rejects_generic_queries(self):
        payload = {
            "queries": [
                {"rank": 1, "query": "viral videos"},
                {"rank": 2, "query": "dublin teen streetwear haul"},
                {"rank": 3, "query": "cork gaa training vlog"},
                {"rank": 4, "query": "irish leaving cert reaction"},
                {"rank": 5, "query": "galway summer festival vlog"},
            ]
        }
        with self.assertRaises(QueryGenerationError):
            normalize_query_payload(payload)

    def test_build_query_user_message_includes_trend_context(self):
        message = build_query_user_message(
            user_prompt="Irish teen fashion and school culture",
            trend_terms=[
                {
                    "term": "junior cert results",
                    "rank": 1,
                    "trend_type": "rising",
                    "date": "2026-06-01",
                    "source": "google_trends_rss",
                }
            ],
        )
        self.assertIn("Irish teen fashion", message)
        self.assertIn("junior cert results", message)
        self.assertIn("Target region: IE", message)
        self.assertIn("source=", message)

    def test_parse_google_trends_rss_preserves_raw_fields(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <rss xmlns:ht="https://trends.google.com/trending/rss"><channel>
          <item>
            <title>Junior Cert results</title>
            <ht:approx_traffic>2K+</ht:approx_traffic>
            <pubDate>Wed, 15 Jul 2026 10:00:00 GMT</pubDate>
            <link>https://trends.google.com/trending?geo=IE</link>
          </item>
        </channel></rss>"""
        raw_items = parse_google_trends_rss(xml)
        self.assertEqual(raw_items[0]["title"], "Junior Cert results")
        self.assertEqual(raw_items[0]["approx_traffic"], "2K+")

        terms = normalize_rss_items(raw_items + raw_items, region="IE")
        self.assertEqual(len(terms), 1)
        self.assertEqual(terms[0]["source"], "google_trends_rss")
        self.assertEqual(terms[0]["score"], "2K+")

    def test_parse_google_trends_csv_detects_header_and_deduplicates(self):
        csv_data = """Exported from Google Trends

Trends,Search volume,Started,Explore link
CAO points,5K+,2 hours ago,https://trends.google.com/explore?q=CAO
cao points,4K+,3 hours ago,https://trends.google.com/explore?q=CAO
Electric Picnic lineup,2K+,4 hours ago,https://trends.google.com/explore?q=EP
"""
        explore_url = build_google_trends_explore_url("Irish teen fashion")
        bundle = parse_google_trends_csv(
            csv_data,
            region="IE",
            filename="trends.csv",
            seed_query="Irish teen fashion",
            source_page_url=explore_url,
        )
        self.assertEqual(bundle["detected_header_row"], 3)
        self.assertEqual(bundle["source_filename"], "trends.csv")
        self.assertEqual(bundle["seed_query"], "Irish teen fashion")
        self.assertEqual(bundle["search_property"], "youtube")
        self.assertEqual(
            [item["term"] for item in bundle["terms"]],
            ["CAO points", "Electric Picnic lineup"],
        )
        self.assertEqual(bundle["terms"][0]["source"], "google_trends_csv")

    def test_build_google_trends_explore_url_uses_interest_and_youtube_search(self):
        url = build_google_trends_explore_url(
            "Irish teen fashion",
            region="IE",
            language="en",
        )
        self.assertIn("q=Irish+teen+fashion", url)
        self.assertIn("geo=IE", url)
        self.assertIn("gprop=youtube", url)
        self.assertIn("date=now+7-d", url)

    def test_top_normal_videos_adds_display_rank_and_preserves_raw_position(self):
        results = [
            {"video_id": "short", "label": "short", "raw_position": 1},
            {"video_id": "a", "label": "normal_video", "raw_position": 3},
            {"video_id": "b", "label": "normal_video", "raw_position": 31},
        ]
        top = top_normal_videos(results, limit=2)
        self.assertEqual([item["display_rank"] for item in top], [1, 2])
        self.assertEqual([item["raw_position"] for item in top], [3, 31])

    def test_filter_excluded_videos(self):
        results = [{"video_id": "a"}, {"video_id": "b"}, {"video_id": "c"}]
        filtered, filtered_out = filter_excluded_videos(results, {"b"})
        self.assertEqual([item["video_id"] for item in filtered], ["a", "c"])
        self.assertEqual(filtered_out, ["b"])

    def test_video_id_from_url(self):
        self.assertEqual(
            video_id_from_url("https://m.youtube.com/watch?v=abc123&feature=share"),
            "abc123",
        )
        self.assertEqual(
            video_id_from_url("https://m.youtube.com/shorts/short123"),
            "short123",
        )

    def test_first_page_is_retried_only_after_a_network_failure(self):
        page = mock.Mock()
        page.goto.side_effect = [Exception("Page.goto: net::ERR_QUIC_PROTOCOL_ERROR at https://m.youtube.com/"), None]
        open_first_page(page, "https://m.youtube.com/")
        self.assertEqual(page.goto.call_count, 2)

        page = mock.Mock()
        page.goto.side_effect = TimeoutError("Timeout 12000ms exceeded")
        with self.assertRaises(TimeoutError):
            open_first_page(page, "https://m.youtube.com/")
        self.assertEqual(page.goto.call_count, 1)

    def test_search_url_uses_mobile_youtube(self):
        self.assertIn("m.youtube.com/results", search_url("irish teen fashion"))
        self.assertIn("gl=IE", search_url("irish teen fashion"))

    def test_normalize_feed_items_orders_dedupes_and_drops_playing_video(self):
        def card(label, video_id, top, **extra):
            href = f"/shorts/{video_id}" if label == "short" else f"/watch?v={video_id}&pp=xyz"
            return {"label": label, "video_id": video_id, "href": href, "doc_top": top,
                    "title": f"Title {video_id}", "card_tag": "ytm-video-with-context-renderer", **extra}

        items = [
            card("normal_video", "current", 100),
            card("normal_video", "abc", 200, duration_text="12:34"),
            card("short", "abc", 250),
            {"label": "ad", "video_id": None, "href": None, "doc_top": 300, "title": "Sponsored"},
            card("short", "def", 400),
            card("normal_video", "ghi", 500, duration_text="1:02:03"),
            card("normal_video", "jkl", 600),
        ]
        results = normalize_feed_items(
            items,
            source="watch_recommendations",
            page_kind="watch",
            current_video_id="current",
            viewport_height=320,
            max_candidates=2,
        )
        self.assertEqual(
            [(item["label"], item["video_id"]) for item in results],
            [("normal_video", "abc"), ("ad", None), ("short", "def"), ("normal_video", "ghi")],
        )
        self.assertEqual([item["raw_position"] for item in results], [1, 2, 3, 4])
        self.assertEqual(results[0]["url"], "https://m.youtube.com/watch?v=abc&pp=xyz")
        self.assertEqual(results[0]["canonical_url"], "https://m.youtube.com/watch?v=abc")
        self.assertEqual(results[0]["duration_seconds"], 754)
        self.assertEqual(results[3]["duration_seconds"], 3723)
        self.assertEqual(results[2]["url"], "https://m.youtube.com/shorts/def")
        self.assertEqual([item["in_first_screen"] for item in results], [True, True, False, False])

    def test_parse_duration_text(self):
        self.assertEqual(parse_duration_text("0:35"), 35)
        self.assertEqual(parse_duration_text("10:00"), 600)
        self.assertIsNone(parse_duration_text("LIVE"))
        self.assertIsNone(parse_duration_text(None))

    def test_assign_display_ranks_numbers_only_normal_videos(self):
        ranked = assign_display_ranks(
            [
                {"label": "short", "video_id": "s", "raw_position": 1},
                {"label": "normal_video", "video_id": "a", "raw_position": 2},
                {"label": "normal_video", "video_id": "b", "raw_position": 3},
            ]
        )
        self.assertEqual([item.get("display_rank") for item in ranked], [None, 1, 2])
        selected, selection = select_next_video(
            [item for item in ranked if item["label"] == "normal_video"], "top_1"
        )
        self.assertEqual(selection["selected_display_rank"], 1)
        self.assertEqual(selection["selected_raw_position"], 2)

    def test_excluded_ids_for_continuation_follow_the_parent_policy(self):
        session = {
            "settings": {"exclude_policy": "displayed"},
            "initial_excluded_video_ids": ["start"],
            "rounds": [
                {"round": 1, "raw_results": [{"video_id": "a"}, {"video_id": "b"}],
                 "selected_result": {"video_id": "a"}},
                {"round": 2, "raw_results": [{"video_id": "c"}, {"video_id": None}],
                 "selected_result": {"video_id": "c"}},
            ],
        }
        self.assertEqual(excluded_ids_for_continuation(session), ["a", "b", "c", "start"])
        self.assertEqual(excluded_ids_for_continuation(session, through_round=1), ["a", "b", "start"])
        session["settings"]["exclude_policy"] = "watched"
        self.assertEqual(excluded_ids_for_continuation(session), ["a", "c", "start"])
        # A branch from round 2 replaces round 2's pick, which it never watched.
        self.assertEqual(excluded_ids_for_continuation(session, through_round=2), ["a", "start"])

    def test_ads_and_mixes_do_not_hide_the_organic_card_of_the_same_video(self):
        items = [
            {"label": "mix", "video_id": "X", "href": "/watch?v=X&list=RDX", "doc_top": 1},
            {"label": "ad", "video_id": "Y", "href": "/watch?v=Y", "doc_top": 2},
            {"label": "normal_video", "video_id": "X", "href": "/watch?v=X", "doc_top": 3},
            {"label": "normal_video", "video_id": "Y", "href": "/watch?v=Y", "doc_top": 4},
            {"label": "short", "video_id": "Y", "href": "/shorts/Y", "doc_top": 5},
            {"label": "mix", "video_id": "current", "href": "/watch?v=current&list=RDcurrent", "doc_top": 6},
        ]
        results = normalize_feed_items(
            items,
            source="watch_recommendations",
            page_kind="watch",
            current_video_id="current",
            viewport_height=100,
            max_candidates=20,
        )
        self.assertEqual(
            [(item["label"], item["video_id"]) for item in results],
            [("mix", "X"), ("ad", "Y"), ("normal_video", "X"), ("normal_video", "Y"), ("mix", "current")],
        )

    def test_count_normal_candidates_matches_what_normalize_keeps(self):
        feed = {
            "current_video_id": "current",
            "items": [
                {"label": "normal_video", "video_id": "current"},
                {"label": "normal_video", "video_id": "a"},
                {"label": "normal_video", "video_id": "a"},
                {"label": "short", "video_id": "b"},
                {"label": "normal_video", "video_id": "c"},
            ],
        }
        self.assertEqual(count_normal_candidates(feed), 2)

        # A normal card whose id appeared earlier as a Short or live card is not counted,
        # exactly as normalize_feed_items drops it.
        feed = {
            "current_video_id": None,
            "items": [
                {"label": "short", "video_id": "X", "href": "/shorts/X"},
                {"label": "normal_video", "video_id": "X", "href": "/watch?v=X"},
                {"label": "live", "video_id": "Y", "href": "/watch?v=Y"},
                {"label": "normal_video", "video_id": "Y", "href": "/watch?v=Y"},
                {"label": "normal_video", "video_id": "A", "href": "/watch?v=A"},
            ],
        }
        kept = normalize_feed_items(
            feed["items"],
            source="search_results",
            page_kind="search",
            current_video_id=None,
            viewport_height=100,
            max_candidates=10,
        )
        self.assertEqual(count_normal_candidates(feed), sum(1 for i in kept if i["label"] == "normal_video"))

    def test_enrich_browser_results_prefers_api_metadata(self):
        class FakeClient:
            def video_metadata_by_id(self, video_ids):
                self.video_ids = video_ids
                return {
                    "abc": {
                        "video_id": "abc",
                        "title": "API Title",
                        "url": "https://www.youtube.com/watch?v=abc",
                        "channel_name": "API Channel",
                        "channel_id": "channel-1",
                        "published_at": "2026-01-01T00:00:00Z",
                        "duration_seconds": 123,
                        "view_count": 10,
                        "like_count": 2,
                        "comment_count": 1,
                    }
                }

        results = [
            {
                "position": 1,
                "label": "normal_video",
                "title": "Wrong DOM Title",
                "url": "https://m.youtube.com/watch?v=abc",
                "video_id": "abc",
                "channel_name": "Wrong DOM Channel",
                "source": "search_results",
                "metadata_source": "browser_dom",
            }
        ]
        enriched, error = enrich_browser_results(results, FakeClient())
        self.assertIsNone(error)
        self.assertEqual(enriched[0]["title"], "API Title")
        self.assertEqual(enriched[0]["channel_name"], "API Channel")
        self.assertEqual(enriched[0]["position"], 1)
        self.assertEqual(enriched[0]["raw_position"], 1)
        self.assertEqual(enriched[0]["label"], "normal_video")
        self.assertEqual(enriched[0]["metadata_source"], "youtube_api")
        self.assertEqual(enriched[0]["browser_observation"]["observed_title"], "Wrong DOM Title")
        # The observed URL is kept for navigation; the API's canonical URL is stored separately.
        self.assertEqual(enriched[0]["url"], "https://m.youtube.com/watch?v=abc")
        self.assertEqual(enriched[0]["canonical_url"], "https://m.youtube.com/watch?v=abc")

    def test_enrichment_failure_never_ends_the_run(self):
        class BrokenClient:
            def video_metadata_by_id(self, video_ids):
                raise RuntimeError("boom https://x/videos?id=a&key=AIzaSyFAKEFAKEFAKEFAKEFAKE12")

        results = [{"video_id": "abc", "label": "normal_video", "title": "DOM"}]
        enriched, error = enrich_browser_results(results, BrokenClient())
        self.assertEqual(enriched, results)
        self.assertIn("boom", error)
        self.assertNotIn("AIzaSyFAKE", error)

    def test_youtube_network_error_does_not_leak_the_key(self):
        def fail(*args, **kwargs):
            raise requests.ConnectionError(
                "Max retries exceeded with url: /youtube/v3/videos?id=abc&key=SECRET_KEY_123"
            )

        client = YouTubeClient("SECRET_KEY_123")
        with mock.patch("recpath.youtube.requests.get", side_effect=fail):
            with self.assertRaises(YouTubeApiError) as caught:
                client.get_videos(["abc"])
        self.assertNotIn("SECRET_KEY_123", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_redact_secrets(self):
        text = "GET /v3/videos?part=id&key=abc123&x=1 AIzaSyA1234567890abcdefghijklmn sk-proj-abcdefghijklmnop1234"
        redacted = redact_secrets(text)
        self.assertNotIn("abc123", redacted)
        self.assertNotIn("AIzaSyA1234567890", redacted)
        self.assertNotIn("sk-proj-abcdefghijklmnop", redacted)
        self.assertIn("x=1", redacted)
        self.assertTrue(error_summary(ValueError("key=zzz")).startswith("ValueError: key=REDACTED"))
        self.assertNotIn("abc", redact_secrets("x-goog-api-key: abc123 apikey=abc123"))
        # Ordinary words are left alone.
        for text in ("monkey=1", "Desk-organization-ideas-for-2024", "task-runner-configuration-x"):
            self.assertEqual(redact_secrets(text), text)

    def test_registered_secrets_are_redacted_whatever_their_format(self):
        self.addCleanup(safety._REGISTERED_SECRETS.clear)
        register_secrets(["plainkey-0123456789", None, "short"])
        self.assertEqual(redact_secrets("{'k': 'plainkey-0123456789'}"), "{'k': 'REDACTED'}")
        self.assertEqual(redact_secrets("short"), "short")

    def test_parse_runtime_config_reads_gcp_credentials(self):
        config = parse_runtime_config(
            [
                "--gcp-credentials-path",
                "/tmp/service-account.json",
                "--gcp-project-id",
                "billing-project",
            ]
        )
        self.assertEqual(config.gcp_credentials_path, "/tmp/service-account.json")
        self.assertEqual(config.gcp_project_id, "billing-project")

    def test_parse_runtime_config_reads_keys_from_environment(self):
        environ = {"YOUTUBE_API_KEY": "env-yt", "OPENAI_API_KEY": "env-openai", "OPENAI_MODEL": "m1"}
        config = parse_runtime_config([], environ=environ)
        self.assertEqual(config.youtube_api_key, "env-yt")
        self.assertEqual(config.openai_api_key, "env-openai")
        self.assertEqual(config.openai_model, "m1")
        override = parse_runtime_config(["--youtube-api-key", "cli-yt"], environ=environ)
        self.assertEqual(override.youtube_api_key, "cli-yt")
        self.assertIsNone(parse_runtime_config([], environ={}).youtube_api_key)

    def test_storage_exports_the_full_feed_and_round_trips(self):
        session = {
            "schema_version": 2,
            "session_id": "session_test",
            "mode": "browser_observation",
            "region": "IE",
            "language": "en",
            "status": "completed",
            "settings": {"device": "Pixel 7", "consent_choice": "accept_all", "exclude_policy": "displayed"},
            "rounds": [
                {
                    "round": 1,
                    "source": "search_results",
                    "filtered_out_video_ids": ["seen"],
                    "raw_results": [
                        {"video_id": "seen", "label": "normal_video", "raw_position": 1},
                        {"video_id": "a", "label": "normal_video", "raw_position": 2},
                        {"video_id": None, "label": "ad", "raw_position": 3},
                    ],
                    "results": [
                        {"video_id": "a", "label": "normal_video", "raw_position": 2, "display_rank": 1},
                        {"video_id": None, "label": "ad", "raw_position": 3},
                    ],
                    "top_normal_videos": [],
                    "selected_result": {"video_id": "a"},
                    "selection": {"strategy": "top_1"},
                    "navigation": {"method": "tap"},
                }
            ],
        }
        rows = flatten_session_rows(session)
        self.assertEqual([row["video_id"] for row in rows], ["seen", "a", None])
        self.assertEqual([row["excluded_as_seen"] for row in rows], [True, False, False])
        self.assertEqual([row["is_selected"] for row in rows], [False, True, False])
        self.assertEqual(rows[1]["display_rank"], 1)
        self.assertEqual(rows[1]["navigation_method"], "tap")
        self.assertEqual(rows[0]["region"], "IE")
        with tempfile.TemporaryDirectory() as temp:
            json_path, csv_path = save_session(session, data_dir=Path(temp))
            self.assertEqual(json.loads(json_path.read_text())["session_id"], "session_test")
            self.assertTrue(csv_path.read_text().startswith("session_id,"))
            self.assertEqual(sorted(p.name for p in Path(temp).iterdir()), ["session_test.csv", "session_test.json"])

    def test_normalize_trend_rows_deduplicates_terms(self):
        rows = [
            {
                "term": "Junior Cert results",
                "trend_rank": 1,
                "trend_score": "100",
                "trend_date": "2026-06-01",
                "source_table": "international_top_terms",
            },
            {
                "term": "junior cert results",
                "trend_rank": 2,
                "trend_score": "90",
                "trend_date": "2026-06-01",
                "source_table": "international_top_terms",
            },
            {
                "term": "CAO points",
                "trend_rank": 3,
                "trend_score": "80",
                "trend_date": "2026-06-01",
                "source_table": "international_top_terms",
            },
        ]
        terms = normalize_trend_rows(rows, table_id="international_top_terms", region="IE")
        self.assertEqual([item["term"] for item in terms], ["Junior Cert results", "CAO points"])
        self.assertEqual(terms[0]["source"], "google_trends_bigquery")


if __name__ == "__main__":
    unittest.main()
