"""YouTube's own page data (InnerTube JSON) as a second source next to the DOM.

The page loads its feeds and player data from /youtubei/v1/{search,next,player,get_watch,reel/...}
(mobile web fetches a watch page as one get_watch list of player + next responses) and, on full
page loads, embeds the first batch as window.ytInitialData / ytInitialPlayerResponse. The
recorder listens to those responses and indexes every video item by id, so DOM results can be
filled in (channel id, view count text, Shorts endpoint) without a YouTube Data API key. The DOM
stays the record of what was visible and where; this only adds fields.
"""
from __future__ import annotations

import json
import re
from typing import Any

ENDPOINT_RE = re.compile(r"/youtubei/v1/(search|next|player|get_watch|reel/reel_item_watch|reel/reel_watch_sequence)\b")
VIDEO_RENDERERS = ("videoWithContextRenderer", "compactVideoRenderer", "videoRenderer")
SHORTS_RENDERERS = ("shortsLockupViewModel", "reelItemRenderer")
MAX_PLAYER_RESPONSES = 40


def text_of(node: Any) -> str | None:
    if not isinstance(node, dict):
        return None
    if "simpleText" in node:
        return node["simpleText"]
    if "runs" in node:
        return "".join(run.get("text", "") for run in node["runs"] if isinstance(run, dict)) or None
    if "content" in node:
        return node["content"]
    return None


def walk_renderers(node: Any, found: list[tuple[str, dict[str, Any]]]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in VIDEO_RENDERERS + SHORTS_RENDERERS and isinstance(value, dict):
                found.append((key, value))
            walk_renderers(value, found)
    elif isinstance(node, list):
        for value in node:
            walk_renderers(value, found)


def describe_renderer(kind: str, renderer: dict[str, Any]) -> dict[str, Any] | None:
    if kind in SHORTS_RENDERERS:
        endpoint = (
            ((renderer.get("onTap") or {}).get("innertubeCommand") or {}).get("reelWatchEndpoint")
            or (renderer.get("navigationEndpoint") or {}).get("reelWatchEndpoint")
            or {}
        )
        video_id = endpoint.get("videoId") or renderer.get("videoId")
        if not video_id:
            return None
        return {"video_id": video_id, "renderer": kind, "endpoint": "reel"}

    video_id = renderer.get("videoId")
    if not video_id:
        return None
    navigation = renderer.get("navigationEndpoint") or {}
    byline = ((renderer.get("shortBylineText") or renderer.get("longBylineText") or {}).get("runs") or [{}])[0]
    styles = [
        (overlay.get("thumbnailOverlayTimeStatusRenderer") or {}).get("style")
        for overlay in renderer.get("thumbnailOverlays") or []
        if isinstance(overlay, dict)
    ]
    return {
        "video_id": video_id,
        "renderer": kind,
        "endpoint": "reel" if "reelWatchEndpoint" in navigation else "watch",
        "channel_id": ((byline.get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get("browseId"),
        "channel_name": byline.get("text"),
        "length_text": text_of(renderer.get("lengthText")),
        "view_count_text": text_of(renderer.get("shortViewCountText") or renderer.get("viewCountText")),
        "published_text": text_of(renderer.get("publishedTimeText")),
        "style": next((style for style in styles if style), None),
    }


class InnerTubeRecorder:
    """Collects InnerTube items and player responses while the runner browses."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.player_responses: dict[str, dict[str, Any]] = {}
        self.responses_seen = 0
        self.errors = 0

    def attach(self, context: Any) -> None:
        context.on("response", self.on_response)

    def on_response(self, response: Any) -> None:
        match = ENDPOINT_RE.search(response.url)
        if not match:
            return
        try:
            payload = parse_json_parts(response.text())
        except Exception:
            self.errors += 1
            return
        self.responses_seen += 1
        self.ingest(payload)

    def ingest(self, payload: Any) -> None:
        if isinstance(payload, list):
            for part in payload:
                self.ingest(part)
            return
        if not isinstance(payload, dict):
            return
        player = payload.get("playerResponse") if "playerResponse" in payload else payload
        video_id = ((player or {}).get("videoDetails") or {}).get("videoId") if isinstance(player, dict) else None
        if video_id and ("adSlots" in player or "adPlacements" in player or "streamingData" in player):
            self.player_responses[video_id] = player
            while len(self.player_responses) > MAX_PLAYER_RESPONSES:
                self.player_responses.pop(next(iter(self.player_responses)))
        found: list[tuple[str, dict[str, Any]]] = []
        walk_renderers(payload, found)
        for kind, renderer in found:
            item = describe_renderer(kind, renderer)
            if item:
                merged = self.items.get(item["video_id"], {})
                # A Shorts renderer or reel endpoint is the stronger evidence; keep it once seen.
                if merged.get("endpoint") == "reel":
                    item["endpoint"] = "reel"
                self.items[item["video_id"]] = {**merged, **{k: v for k, v in item.items() if v is not None}}

    def ingest_page_globals(self, page: Any) -> None:
        """On full page loads the first data batch is embedded in the page, not fetched."""
        try:
            data = page.evaluate(
                "() => ({initial: window.ytInitialData || null, player: window.ytInitialPlayerResponse || null})"
            )
        except Exception:
            return
        self.ingest(data.get("initial"))
        self.ingest(data.get("player"))

    def player_response(self, video_id: str | None, page: Any = None) -> dict[str, Any] | None:
        """The player data for a video; if no response was captured, ask the page's player."""
        if video_id and video_id not in self.player_responses and page is not None:
            try:
                self.ingest(page.evaluate(
                    "() => { const p = document.querySelector('#movie_player');"
                    " try { return p && p.getPlayerResponse ? p.getPlayerResponse() : null; } catch (e) { return null; } }"
                ))
            except Exception:
                pass
        return self.player_responses.get(video_id) if video_id else None


def parse_json_parts(text: str) -> Any:
    """Parse a JSON body, or a stream of concatenated JSON values (returned as a list)."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    parts, index = [], 0
    while index < len(text):
        while index < len(text) and text[index] in " \t\r\n,":
            index += 1
        if index >= len(text):
            break
        value, index = decoder.raw_decode(text, index)
        parts.append(value)
    if not parts:
        raise ValueError("no JSON in response")
    return parts


def enrich_from_innertube(
    results: list[dict[str, Any]],
    recorder: InnerTubeRecorder | None,
) -> list[dict[str, Any]]:
    """Attach InnerTube fields to DOM results by video id (copies; DOM fields win)."""
    if recorder is None:
        return results
    enriched = []
    for result in results:
        item = recorder.items.get(result.get("video_id") or "")
        if not item:
            enriched.append(result)
            continue
        copy = result.copy()
        copy["innertube"] = {
            key: item.get(key)
            for key in ("channel_id", "view_count_text", "published_text", "length_text", "style", "renderer", "endpoint")
        }
        copy["channel_id"] = copy.get("channel_id") or item.get("channel_id")
        enriched.append(copy)
    return enriched
