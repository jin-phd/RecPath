# Technical reference

How a run works, what is recorded, and where. For installation and a first walkthrough see the [README](../README.md).

- [How a recommendation-chain run works](#how-a-recommendation-chain-run-works)
- [Content location](#content-location)
- [Shorts feed runs](#shorts-feed-runs)
- [Ads](#ads)
- [Shorts](#shorts)
- [Safeguards](#safeguards)
- [Selection and exclusion](#selection-and-exclusion)
- [Metadata sources](#metadata-sources)
- [Data model](#data-model)
- [CSV export](#csv-export)
- [Batch runs](#batch-runs)
- [API metadata mode](#api-metadata-mode)
- [Query ideas: OpenAI and Google Trends](#query-ideas-openai-and-google-trends)
- [Configuration](#configuration)
- [Code map](#code-map)
- [Safety and scope](#safety-and-scope)

## How a recommendation-chain run works

The browser is Playwright Chromium with a phone profile (Pixel 7 by default, iPhone 13 optional), `en-IE` locale and the `Europe/Dublin` time zone, opening `m.youtube.com`. It is a device *emulation*, not an Android or iOS emulator. Every run starts as a new anonymous, logged-out visitor with a fresh browser profile. QUIC (HTTP/3) is switched off (`--disable-quic`, listed in `client.browser_args`), so pages load over HTTP/2: VPNs, proxies and firewalls often break QUIC part-way, which Chromium reports as `net::ERR_QUIC_PROTOCOL_ERROR`. YouTube picks the region from the IP address unless a content location is chosen (see [Content location](#content-location)); the region and language it actually used are recorded from its own client config (`client.innertube_client`).

1. Set the content location (the `PREF` cookie), then open the search results for the start query (or, for a continuation, the start video's watch page). If the network drops this first load (a `net::ERR_...` code), it is tried once more after 2 s; a second failure ends the run with a `NetworkError` that names the code.
2. Answer the EU cookie dialog with the configured choice (`Accept all` by default, or `Reject all`) and record what was clicked. A click that lands before the dialog is wired up is retried once (`retried` on the consent event).
3. For each round:
   1. Check the content location YouTube applies on the page (`round.location`), then take the "on arrival" screenshot.
   2. On a watch page, stay on the video for the watch time (`watch_dwell_ms`, 6 s by default) while sampling the player for ads. A stop request cuts the watch short (`watch_cut_short` on the round).
   3. Read the feed card by card: the search results, or every `related-items` section of a watch page. The top-bar "Open App" link, hidden chapter links, the companion ad slot and the playing video itself are never counted. Scroll to the end of the feed so YouTube loads more, until the scan depth (number of normal videos) is reached or loading stops.
   4. Label each card (`normal_video`, `short`, `ad`, `live`, `upcoming`, `movie`, `playlist`, `mix`, `channel`), join YouTube's page data and, if a key is configured, YouTube Data API metadata.
   5. Select the next normal video (`top_1` or `random`), screenshot the selected card with a red outline, and save the round.
   6. Tap the selected card (falling back to opening its URL), and wait until the watch page and its related list belong to the new video.
4. Save JSON, CSV and screenshots after every round. Errors end the run but keep the finished rounds (`status: "error"`).

A stop request is honoured within seconds: the current watch is cut short, the round's list is still read and saved, and the run ends with `end_reason: "stopped"` (also when it arrives in the last round).

Progress is reported live to the UI through events (`started`, `step`, `round`, `navigation`, `short`, `finished`), a stop request is honoured between steps, and the phone screen is mirrored as JPEG frames (CDP screencast).

## Content location

YouTube's Location setting (`gl`) decides which country's charts, news and results it localises for. The New run page and the batch runner choose it per run (`settings.region`: `IE` by default, `GB` or `US`). The runner stores it the way YouTube's own Location menu does: as `gl=<code>` in the `PREF` cookie, set before the first page load (`settings.location_cookie`, on by default). Choosing United Kingdom in the logged-out desktop Location menu writes the same `gl=GB` into `PREF`. The search URL carries `gl` too, but YouTube drops URL parameters when the cookie dialog reloads the page, so without the cookie the IP's location applies. The IP address, `en-IE` locale, `en` language and `Europe/Dublin` time zone do not change: a run with location `GB` is an Irish visitor who chose the UK, not a visitor in the UK.

Every round records `location` = `{requested, gl, hl, pref_gl, ok}`: the requested code, the `gl`/`hl` of YouTube's client config on that page, and the `gl` in the `PREF` cookie sent with every request. When YouTube applied another location, the run ends with `end_reason: "location_mismatch"` before the round is read; `ok` is `null` when a page shows no client config to compare with. A Shorts feed checks it once, after the cookie dialog (`shorts_start.location`). Sessions saved before this setting existed record no `location`; they ran in IE without the cookie.

## Shorts feed runs

`run_shorts_feed` opens the first Short shown for the start query (or the Shorts tab if there is none), then swipes through the Shorts feed for the chosen number of items. Each item records its video id, title, channel, a screenshot, how long it was watched and whether it was an ad (with advertiser, headline and the call-to-action's host). Ads are never tapped. The session has `mode: "shorts_feed"` and its items are in `shorts_feed`.

## Ads

On every watch page the runner records, in `round.ads`:

| Field | What it is |
| --- | --- |
| `player` | Whether an in-player ad appeared (`ad_seen`, `first_seen_ms`), and each ad of the pod: title, `pod_position` ("1 of 2"), advertiser host, skippable. Samples are merged into the ad currently playing unless a known title, advertiser or pod position differs, so one ad is counted once even when its label renders late or shortens to "Sponsored". A frame is saved as `screenshots.ad` once the ad has rendered (or 1 s after it started; `screenshot_ms`). `null` on search rounds, where no player is watched: player ads are then unknown, not zero. |
| `companion` | The companion ad under the player: headline, description, call-to-action host (click trackers without a destination are ignored) and `advertiser`, the display domain shown on the ad. |
| `scheduled` | The ads YouTube scheduled for this video in its player data: `preroll`, `midroll` (with `offset_ms`), `postroll`, `below_player`, `in_player`, with ad video ids and short display texts. Tracking URLs are not kept. |
| `paid_promotion` | The video carries an "Includes paid promotion" disclosure. |
| `feed_ads` | Ad cards in the feed (search or related list). |
| `beacons` | Counts of ad impression/view requests the page sent (`/api/stats/ads`, `/pagead/adview`, `/pcs/activeview`, `/pagead/interaction`). |

What is served depends on the consent answer (personalised vs non-personalised ads), the device and chance; scheduled ads are not necessarily shown within the watch time. In-feed search ads were not observed on mobile web during development.

## Shorts

Since October 2024 a Short can be up to 3 minutes long, and the Data API has no "is Short" flag, so duration alone never decides. A card is labelled `short` from these signals, each listed in `shorts_evidence`:

- `dom:shorts_link` / `dom:shorts_card` / `dom:shorts_badge`: a `/shorts/` link, a Shorts card or a SHORTS badge on the page.
- `innertube:reel_endpoint`: YouTube's page data opens the video in the Shorts player.
- `shorts_url:200` / `shorts_url:redirect`: for a normal-looking video of at most 3 minutes, YouTube's own answer for `/shorts/<id>` (200 for a Short, a redirect otherwise). At most 10 checks per round, cached per run (`check_shorts_urls`).

`presentation` says how it was shown (`shorts_shelf`, `shorts_card`, `video_card` — a Short shown as a normal card —, `shelf`, `other`) and `duration_bucket` gives `<=60s`, `61-180s` or `>180s`.

## Safeguards

A run ends rather than recording misleading data when:

- the cookie dialog cannot be answered with the requested choice (`consent.applied` false; the failed attempt is recorded);
- YouTube applies another content location than the requested one (`end_reason: "location_mismatch"`; see [Content location](#content-location));
- the page has no recommendation list (`end_reason: "feed_not_found"`), e.g. after a YouTube layout change;
- after navigating, the related list never changes from the previous video's, even after one reload;
- navigation lands on a different video. A tap that opens something else is recorded (`tap_landed_url`) and replaced by opening the right URL.

A failed save never stops a run (the error is listed in `save_errors`); the session stays available for download in the UI.

## Selection and exclusion

| Strategy | Behaviour | Recorded |
| --- | --- | --- |
| `top_1` | First eligible normal video in feed order. | Display rank, raw position, reason. |
| `random` | A random eligible normal video. | Random seed, candidate count. |

The exclude policy decides which videos can no longer be selected: `displayed` (default) excludes every video already shown in an earlier round; `watched` excludes only the videos the chain selected. Continuations and branches inherit the parent's exclusions up to the round they start from and run with the parent's device, consent, navigation, exclusion and scan-depth settings. A branch from round r does not inherit round r's own pick under `watched`.

## Metadata sources

- The browser page (DOM) is the record of what was shown and where: titles, channels, durations, positions.
- YouTube's page data (InnerTube JSON) adds channel ids, view count text and Shorts endpoints (`result.innertube`), without an API key.
- With a YouTube Data API key, `videos.list` fills authoritative metadata (publish date, exact counts, tags). `metadata_source` says which applied; the observed title/channel/URL are kept in `browser_observation`.

## Data model

Sessions are JSON files in `data/sessions/` (one per run, plus a CSV); screenshots are JPEGs in `data/screenshots/`. `schema_version` is 3 for current sessions; older sessions (no version, or 2) remain readable in the library.

### Session

```json
{
  "schema_version": 3,
  "session_id": "session_20261022_101500_123456",
  "created_at": "2026-10-22T10:15:00Z",
  "created_at_local": "2026-10-22 11:15:00 IST",
  "finished_at": "2026-10-22T10:16:10Z",
  "mode": "browser_observation",
  "status": "completed",
  "end_reason": "max_rounds",
  "error": null,
  "start_query": "irish teen fashion trends",
  "start_url": null,
  "strategy": "top_1",
  "settings": {"device": "Pixel 7", "region": "GB", "location_cookie": true, "locale": "en-IE", "consent_choice": "accept_all", "navigation": "tap", "exclude_policy": "displayed", "watch_dwell_ms": 6000, "max_rounds": 5, "max_candidates": 20},
  "client": {"user_agent": "...", "ua_brands": ["Chromium 148"], "innertube_client": {"client_name": "MWEB", "gl": "IE", "hl": "en"}},
  "consent": {"choice": "accept_all", "checked": true, "applied": true, "events": [{"surface": "in_page_dialog", "clicked_label": "Accept the use of cookies ...", "ok": true}]},
  "innertube_capture": {"responses": 8, "items_indexed": 109, "player_responses": ["..."]},
  "parent": null,
  "initial_excluded_video_ids": [],
  "batch": null,
  "rounds": []
}
```

`status`: `running` (while checkpoints are written), `completed`, `error`, `interrupted`. `end_reason`: `max_rounds`, `no_candidates`, `feed_not_found`, `stopped` (stopped from the UI), `feed_stuck` (Shorts feed did not advance), `location_mismatch` (see [Content location](#content-location)), `error`, `interrupted`. `batch` is `{batch_id, condition, repeat, label}` for a run started by the batch runner.

Sessions also record `task_type`, `region`, `language`, the query-ideas context (`generated_queries`, `selected_query`, `system_prompt`, `user_prompt`, `openai_model`) and `last_save_ok` / `save_errors`. A continuation or branch has `parent` = `{session_id, round, video_id, title, kind}` with `kind` `continuation` (resumed from that round's own pick) or `branch`. API metadata sessions use the same `schema_version` with `settings` `{strategy, max_rounds, max_results, region, language}`.

### Round

```json
{
  "round": 2,
  "source": "watch_recommendations",
  "page_url": "https://m.youtube.com/watch?v=...",
  "current_video_id": "...",
  "fetched_at": "2026-10-22T10:15:30Z",
  "location": {"requested": "GB", "gl": "GB", "hl": "en", "pref_gl": "GB", "ok": true},
  "screenshot_path": "data/screenshots/session_..._round_02.jpg",
  "screenshots": {"landing": "...round_02.jpg", "selected": "...round_02_selected.jpg", "ad": "...round_02_ad.jpg"},
  "label_counts": {"normal_video": 20, "short": 2},
  "scroll": {"passes": 1, "stalled": false, "normal_candidates_found": 24, "cap_reached": true, "section_count": 3, "unmatched_links": {"count": 0}},
  "ads": {"player": {"ad_seen": true, "ads": [{"title": "...", "pod_position": "1 of 2", "skippable": true}]}, "companion": null, "scheduled": [{"kind": "midroll", "offset_ms": 94666}], "feed_ads": 0, "beacons": {"stats_ads": 1}},
  "navigation": {"method": "tap", "requested_video_id": "...", "landed_video_id": "...", "related_refreshed": true, "reloaded": false},
  "raw_results": [],
  "results": [],
  "top_normal_videos": [],
  "selected_result": {},
  "selection": {"strategy": "top_1", "selected_display_rank": 1}
}
```

`raw_results` is the whole observed feed in order; `results` drops videos excluded as already seen; `top_normal_videos` is the first ten normal videos of `results`.

Screenshot paths are stored relative to the project folder (`data/screenshots/...`). When `RECPATH_DATA_DIR` points outside the project they are absolute, so exported JSON/CSV then contain that local folder path (including your user name).

### Result

```json
{
  "raw_position": 3,
  "display_rank": 2,
  "label": "normal_video",
  "title": "Example Video Title",
  "channel_name": "Example Channel",
  "channel_url": "https://m.youtube.com/@example",
  "channel_id": "UC...",
  "video_id": "abc123def45",
  "url": "https://m.youtube.com/watch?v=abc123def45&pp=...",
  "canonical_url": "https://m.youtube.com/watch?v=abc123def45",
  "duration_text": "12:34",
  "duration_seconds": 754,
  "duration_bucket": ">180s",
  "presentation": "video_card",
  "shorts_evidence": [],
  "innertube": {"channel_id": "UC...", "view_count_text": "1.2K views", "published_text": "2 days ago", "endpoint": "watch"},
  "card_tag": "ytm-video-with-context-renderer",
  "doc_top": 512,
  "in_first_screen": true,
  "metadata_source": "browser_dom"
}
```

## CSV export

The JSON file is the canonical record; the CSV has one row per observed item (`raw_results`, with `excluded_as_seen` marking items removed as already seen) and, for Shorts feed sessions, one row per Short. Columns include the original set (`session_id, mode, round, source, page_url, display_rank, raw_position, label, title, url, video_id, channel_name, channel_id, ..., is_selected, selection_strategy, screenshot_path`) plus `region` (the requested content location) and `location_applied` (the `gl` YouTube applied on that round's page), `schema_version`, `session_status`, `start_query`, `parent_session_id`, `batch_id`, `device`, `consent_choice`, `exclude_policy`, `card_tag`, `observed_url`, `canonical_url`, `channel_url`, `duration_text`, `doc_top`, `in_first_screen`, `navigation_method`, `presentation`, `shorts_evidence`, `duration_bucket`, the per-round ad columns `round_player_ad_seen`, `round_player_ads`, `round_scheduled_ads`, `round_feed_ads`, and `session_end_reason` / `session_planned` (planned rounds, or Shorts for a Shorts feed), so a stopped or early-ended run can be told apart from a full one (`session_status` stays the recorded status).

Titles and channel names are as YouTube showed them, so import the CSV into a spreadsheet as text (a title starting with `=` would otherwise be treated as a formula).

## Batch runs

`python -m recpath.batch design.csv` runs the recommendation chains of a design CSV one after another, without the app. Each row is a condition: `query`, `location` and, optionally, `repeats`, `rounds`, `strategy`, `consent`, `exclude_policy`, `location_cookie`, `category` and `label`; rows without a query are skipped and invalid values are refused before anything runs. Other settings are the runner's defaults (Pixel 7, tap, everything displayed excluded unless `exclude_policy` says otherwise, scan depth 20, 6 s watch, no Data API enrichment); `--rounds` (default 5), `--pacing-ms` (default 250) and `--pause` (seconds between runs, default 10) apply to every run, and `--dry-run` prints the plan.

Runs are interleaved by repeat (repeat 1 of every condition in row order, then repeat 2, ...), so conditions on neighbouring rows run minutes apart. Sessions are saved to `data/sessions/` after every round like app runs, tagged with `batch` (`batch_id`, `condition`, `repeat`, `label`, `category`). `data/batches/<batch_id>.json` holds the design, the git commit the batch ran on (`code_version`, with `uncommitted_changes`), each condition's settings and every run's session id, status, end reason, rounds and duration; it is rewritten after every run. A browser that cannot start ends the batch; any other failure ends only that run. Only one browser run should be active at a time, so do not start a run in the app during a batch.

## API metadata mode

A secondary, API-only mode: it runs a keyword-search chain through the YouTube Data API (search, then a query derived from the selected video's tags/title). It does not see YouTube's recommendation feed (the API no longer offers related-video search), so it is useful for metadata comparison, not for recommendation tracking. In this mode a video of at most 60 s is labelled a Short and 61–180 s is flagged as possibly a Short.

## Query ideas: OpenAI and Google Trends

Optional helpers for choosing a start query:

- **OpenAI**: generates five concrete YouTube search queries from an interest description (plus selected trend terms). The exact system prompt is editable and the exact user message is shown before sending. The model is selectable.
- **Google Trends**: three transparent sources — an interest-based Explore CSV you export from Google Trends and upload unchanged (default), the Ireland Trending Now RSS feed (region-wide baseline), or the BigQuery public dataset (needs GCP credentials). No private endpoints or scraping are used.

## Configuration

Keys go in `.streamlit/secrets.toml` (copy `.streamlit/secrets.example.toml`) or environment variables of the same name (`GOOGLE_APPLICATION_CREDENTIALS` also works for `GCP_CREDENTIALS_PATH`). Command-line flags after `--` also work: `--youtube-api-key`, `--openai-api-key`, `--gcp-credentials-path`, `--gcp-project-id`, `--openai-model`, and `--max-rounds` (the Rounds default, capped at 10), e.g. `streamlit run app.py -- --max-rounds 3`. Flags show keys in `ps` output and shell history, so prefer `secrets.toml` for keys. Order: flags > `secrets.toml` > environment (Streamlit copies `secrets.toml` into the environment at start).

| Name | Needed for |
| --- | --- |
| `YOUTUBE_API_KEY` | Optional metadata enrichment; API metadata mode. |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | Optional query ideas. |
| `GCP_CREDENTIALS_PATH`, `GCP_PROJECT_ID` | Optional Google Trends via BigQuery. |
| `RECPATH_DATA_DIR` | Optional: store sessions and screenshots somewhere other than `data/`. |
| `PLAYWRIGHT_BROWSERS_PATH` | Optional: browsers somewhere other than `.playwright-browsers/`. |

`.streamlit/config.toml` binds the app to `localhost`, so other machines on the network cannot open it.

## Code map

| Path | Role |
| --- | --- |
| `app.py`, `ui/` | Streamlit app: New run, Live run, Session library, Compare. |
| `recpath/browser_observation.py` | The runner: chain and Shorts feed runs, navigation, consent, safeguards. |
| `recpath/js/feed_cards.js` | Reads feed cards from the page. |
| `recpath/js/player_state.js`, `recpath/ads.py` | Player/companion ad sampling, ad schedules, ad beacons. |
| `recpath/js/shorts_item.js` | Reads the Short on screen in the Shorts feed. |
| `recpath/innertube.py` | Records YouTube's page data and joins it to results. |
| `recpath/run_manager.py` | Runs one crawl at a time in a background thread for the UI; continuation/branch settings. |
| `recpath/batch.py` | Command-line batch runner for a design CSV. |
| `recpath/api_tracking.py` | API metadata mode: keyword chain through the YouTube Data API. |
| `recpath/selection.py` | Exclusion, display ranks and the top_1/random pick. |
| `recpath/config.py`, `paths.py`, `safety.py`, `time_utils.py` | Keys and flags; data and browser folders; secret redaction in errors; Dublin time. |
| `recpath/library.py` | Index of saved sessions. |
| `recpath/storage.py` | JSON/CSV writing. |
| `recpath/youtube.py`, `classification.py` | YouTube Data API client and API-mode labels. |
| `recpath/openai_queries.py`, `prompts.py`, `trends.py` | Query ideas. |
| `tests/` | Unit tests, and offline browser tests on pages built from captured YouTube card markup (`tests/fixtures/mweb_cards.json`, `tests/mweb_fixture.py`); skipped if Chromium cannot start. |

## Safety and scope

The runner uses an isolated, logged-out browser profile; it never likes, comments, subscribes or signs in, never taps ads, never solves CAPTCHAs or bypasses access controls, and keeps runs short (5 rounds by default) with human-like pacing. Automated access to YouTube is restricted by its Terms of Service; use the tool for small, documented research observations, ideally under your institution's research-ethics approval, and consider official researcher access (e.g. the YouTube Researcher Program, or DSA Article 40 data access in the EU) for anything at scale.
