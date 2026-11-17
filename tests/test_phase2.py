import copy
import json
import unittest
from pathlib import Path

from recpath.ads import ad_texts, distinct_player_ads, record_ad_sample, same_ad, scheduled_ads
from recpath.browser_observation import classify_shorts
from recpath.innertube import InnerTubeRecorder, enrich_from_innertube, parse_json_parts
from recpath.storage import flatten_session_rows


class FakeChecker:
    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def new_round(self):
        pass

    def is_short(self, video_id):
        self.asked.append(video_id)
        return self.answers.get(video_id)


class AdsTests(unittest.TestCase):
    def test_scheduled_ads_reads_slots_and_placements_without_urls(self):
        player = {
            "adSlots": [
                {"adSlotRenderer": {
                    "adSlotMetadata": {"slotType": "SLOT_TYPE_PLAYER_BYTES", "triggerEvent": "SLOT_TRIGGER_EVENT_BEFORE_CONTENT"},
                    "fulfillmentContent": {"x": {"externalVideoId": "AdVid", "headline": {"simpleText": "Buy shoes"},
                                                 "clickthrough": {"simpleText": "https://ad.example/track"}}},
                }},
                {"adSlotRenderer": {"adSlotMetadata": {"slotType": "SLOT_TYPE_BELOW_PLAYER"}}},
            ],
            "adPlacements": [
                {"adPlacementRenderer": {"config": {"adPlacementConfig": {"kind": "AD_PLACEMENT_KIND_START"}}}},
                {"adPlacementRenderer": {"config": {"adPlacementConfig": {
                    "kind": "AD_PLACEMENT_KIND_MILLISECONDS", "adTimeOffset": {"offsetStartMilliseconds": "94666"}}}}},
                {"adPlacementRenderer": {"config": {"adPlacementConfig": {
                    "kind": "AD_PLACEMENT_KIND_END", "adTimeOffset": {"offsetStartMilliseconds": "-1"}}}}},
            ],
        }
        slots, paid = scheduled_ads(player)
        self.assertEqual([s["kind"] for s in slots], ["preroll", "below_player", "preroll", "midroll", "postroll"])
        self.assertEqual(slots[0]["ad_video_ids"], ["AdVid"])
        self.assertEqual(slots[0]["texts"], ["Buy shoes"])
        self.assertEqual(slots[3]["offset_ms"], 94666)
        self.assertEqual(slots[4]["offset_ms"], -1)
        self.assertFalse(paid)
        self.assertEqual(scheduled_ads(None), ([], None))
        self.assertTrue(scheduled_ads({"paidContentOverlay": {}})[1])

    def test_ad_texts_keeps_short_display_strings_only(self):
        node = {"headline": {"runs": [{"text": "Fast "}, {"text": "delivery"}]},
                "description": {"simpleText": "x" * 200},
                "callToAction": {"content": "https://tracker.example/click"},
                "other": {"simpleText": "not an ad text key"}}
        self.assertEqual(ad_texts(node), ["Fast delivery"])

    def test_same_ad_ignores_the_countdown(self):
        a = {"title": None, "advertiser_host": None, "pod_position": "1 of 2"}
        self.assertTrue(same_ad(a, dict(a)))
        self.assertFalse(same_ad(a, {**a, "pod_position": "2 of 2"}))

    def samples(self, *pods):
        ads = []
        for pod in pods:
            position = pod.split("Sponsored")[-1].strip() if pod and "of" in pod else None
            record_ad_sample(ads, {"title": None, "pod_text": pod, "pod_position": position,
                                   "advertiser_host": None, "skippable": False})
        return ads

    def test_one_playing_ad_is_counted_once_however_its_label_changes(self):
        # Seen live: the label renders late, or shortens to "Sponsored" while the ad plays.
        self.assertEqual(len(self.samples(None, "Sponsored 1 of 2", "Sponsored 1 of 2")), 1)
        self.assertEqual(len(self.samples("Sponsored 1 of 2", "Sponsored")), 1)
        self.assertEqual(len(self.samples("Sponsored", "Sponsored 1 of 2")), 1)
        merged = self.samples(None, "Sponsored 1 of 2")[0]
        self.assertEqual(merged["pod_position"], "1 of 2")

    def test_the_next_ad_of_a_pod_is_counted_separately(self):
        ads = self.samples("Sponsored 1 of 2", "Sponsored", "Sponsored 2 of 2", "Sponsored")
        self.assertEqual([ad["pod_position"] for ad in ads], ["1 of 2", "2 of 2"])

    def test_distinct_player_ads_corrects_older_recordings(self):
        blank = {"title": None, "pod_text": None, "pod_position": None, "advertiser_host": None}
        named = {**blank, "pod_text": "Sponsored 1 of 2", "pod_position": "1 of 2"}
        self.assertEqual(distinct_player_ads([blank, named]), [named])
        self.assertEqual(distinct_player_ads([blank]), [blank])
        self.assertEqual(distinct_player_ads(None), [])


class ShortsTests(unittest.TestCase):
    def test_classify_shorts_combines_signals_and_records_them(self):
        results = [
            {"label": "short", "video_id": "s1", "observed_url": "https://m.youtube.com/shorts/s1",
             "card_tag": "ytm-shorts-lockup-view-model", "shelf": {"tag": "grid-shelf-view-model"}},
            {"label": "normal_video", "video_id": "r1", "observed_url": "https://m.youtube.com/watch?v=r1",
             "card_tag": "ytm-video-with-context-renderer", "duration_seconds": 500,
             "innertube": {"endpoint": "reel"}},
            {"label": "normal_video", "video_id": "q1", "observed_url": "https://m.youtube.com/watch?v=q1",
             "card_tag": "ytm-video-with-context-renderer", "duration_seconds": 150},
            {"label": "normal_video", "video_id": "n1", "observed_url": "https://m.youtube.com/watch?v=n1",
             "card_tag": "ytm-video-with-context-renderer", "duration_seconds": 45},
            {"label": "normal_video", "video_id": "long", "observed_url": "https://m.youtube.com/watch?v=long",
             "card_tag": "ytm-video-with-context-renderer", "duration_seconds": 900},
        ]
        checker = FakeChecker({"q1": True, "n1": False})
        out = classify_shorts(results, checker)
        self.assertEqual([r["label"] for r in out], ["short", "short", "short", "normal_video", "normal_video"])
        self.assertEqual(out[0]["presentation"], "shorts_shelf")
        self.assertEqual(out[1]["shorts_evidence"], ["innertube:reel_endpoint"])
        self.assertEqual(out[1]["presentation"], "video_card")  # a Short shown as a normal card
        self.assertEqual(out[2]["shorts_evidence"], ["shorts_url:200"])
        self.assertEqual(out[3]["shorts_evidence"], ["shorts_url:redirect"])
        self.assertEqual([r["duration_bucket"] for r in out], [None, ">180s", "61-180s", "<=60s", ">180s"])
        self.assertEqual(checker.asked, ["q1", "n1"])  # only short normal-looking videos are checked
        self.assertEqual(results[1]["label"], "normal_video")  # input not modified


class InnerTubeTests(unittest.TestCase):
    def test_recorder_indexes_items_player_responses_and_get_watch_lists(self):
        recorder = InnerTubeRecorder()
        recorder.ingest([
            {"playerResponse": {"videoDetails": {"videoId": "cur"}, "adSlots": []}},
            {"response": {"contents": [
                {"videoWithContextRenderer": {
                    "videoId": "v1",
                    "shortBylineText": {"runs": [{"text": "Chan", "navigationEndpoint": {"browseEndpoint": {"browseId": "UC1"}}}]},
                    "shortViewCountText": {"simpleText": "1.2K views"},
                    "lengthText": {"simpleText": "4:05"},
                    "navigationEndpoint": {"watchEndpoint": {"videoId": "v1"}},
                }},
                {"shortsLockupViewModel": {"onTap": {"innertubeCommand": {"reelWatchEndpoint": {"videoId": "s1"}}}}},
            ]}},
        ])
        self.assertIn("cur", recorder.player_responses)
        self.assertEqual(recorder.items["v1"]["channel_id"], "UC1")
        self.assertEqual(recorder.items["v1"]["endpoint"], "watch")
        self.assertEqual(recorder.items["s1"]["endpoint"], "reel")
        enriched = enrich_from_innertube([{"video_id": "v1", "channel_id": None}, {"video_id": "zz"}], recorder)
        self.assertEqual(enriched[0]["channel_id"], "UC1")
        self.assertEqual(enriched[0]["innertube"]["view_count_text"], "1.2K views")
        self.assertNotIn("innertube", enriched[1])

    def test_lockup_view_model_items_are_indexed(self):
        cards = json.loads((Path(__file__).parent / "fixtures" / "mweb_cards.json").read_text(encoding="utf-8"))
        video = cards["lockup_view_model"]  # captured from a live mobile page
        live = copy.deepcopy(video)
        live["contentId"] = "live0000001"
        live["rendererContext"]["commandContext"]["onTap"]["innertubeCommand"]["watchEndpoint"]["videoId"] = "live0000001"
        badge = live["contentImage"]["thumbnailViewModel"]["overlays"][0]["thumbnailBottomOverlayViewModel"]["badges"][0]
        badge["thumbnailBadgeViewModel"].update(text="LIVE", badgeStyle="THUMBNAIL_OVERLAY_BADGE_STYLE_LIVE")
        reel = copy.deepcopy(video)
        reel["contentId"] = "reel0000001"
        reel["rendererContext"]["commandContext"]["onTap"]["innertubeCommand"] = {
            "reelWatchEndpoint": {"videoId": "reel0000001"}
        }
        playlist = {**copy.deepcopy(video), "contentId": "PLx", "contentType": "LOCKUP_CONTENT_TYPE_PLAYLIST"}
        recorder = InnerTubeRecorder()
        recorder.ingest({"contents": [{"lockupViewModel": item} for item in (video, live, reel, playlist)]})

        item = recorder.items["KlIL63MeyMY"]
        self.assertEqual(item["renderer"], "lockupViewModel")
        self.assertEqual(item["endpoint"], "watch")
        self.assertEqual(item["channel_id"], "UCrWvhVmt0Qac3HgsjQK62FQ")
        self.assertEqual(item["channel_name"], "Curso em Vídeo")
        self.assertEqual(item["length_text"], "37:59")
        self.assertEqual(item["view_count_text"], "1.8m views")
        self.assertEqual(item["published_text"], "10 years ago")
        self.assertEqual(item["style"], "DEFAULT")
        self.assertEqual(recorder.items["live0000001"]["style"], "LIVE")
        self.assertNotIn("length_text", recorder.items["live0000001"])
        self.assertEqual(recorder.items["reel0000001"]["endpoint"], "reel")
        self.assertEqual(set(recorder.items), {"KlIL63MeyMY", "live0000001", "reel0000001"})

    def test_parse_json_parts_handles_streamed_bodies(self):
        self.assertEqual(parse_json_parts('{"a": 1}'), {"a": 1})
        self.assertEqual(parse_json_parts('{"a": 1}\n{"b": 2}'), [{"a": 1}, {"b": 2}])
        with self.assertRaises(ValueError):
            parse_json_parts("not json")


class StorageTests(unittest.TestCase):
    def test_shorts_feed_sessions_flatten_to_one_row_per_short(self):
        session = {
            "session_id": "s", "mode": "shorts_feed", "schema_version": 3, "status": "completed",
            "settings": {"device": "Pixel 7"},
            "shorts_feed": [
                {"position": 1, "video_id": "a", "title": "A", "channel_name": "@a", "is_ad": False, "url": "u1"},
                {"position": 2, "video_id": "b", "title": None, "is_ad": True,
                 "ad": {"headline": "Buy", "advertiser": "Shop"}, "url": "u2"},
            ],
        }
        session["end_reason"] = "stopped"
        session["settings"]["shorts_items"] = 5
        rows = flatten_session_rows(session)
        self.assertEqual([(r["round"], r["label"], r["title"], r["channel_name"]) for r in rows],
                         [(1, "short", "A", "@a"), (2, "ad", "Buy", "Shop")])
        self.assertEqual({(r["session_end_reason"], r["session_planned"]) for r in rows}, {("stopped", 5)})


if __name__ == "__main__":
    unittest.main()
