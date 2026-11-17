from __future__ import annotations

import re
from typing import Any

import requests

from .classification import classify_result, parse_int, parse_iso8601_duration
from .safety import redact_secrets


YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
STOP_WORDS = {
    "about",
    "after",
    "again",
    "and",
    "are",
    "best",
    "for",
    "from",
    "full",
    "how",
    "into",
    "official",
    "that",
    "the",
    "this",
    "video",
    "with",
    "youtube",
}


class YouTubeApiError(RuntimeError):
    pass


class YouTubeClient:
    def __init__(self, api_key: str, region: str = "IE", language: str = "en") -> None:
        self.api_key = api_key
        self.region = region
        self.language = language

    def search_videos(
        self,
        query: str,
        max_results: int = 25,
        order: str = "relevance",
    ) -> dict[str, Any]:
        params = {
            "part": "snippet",
            "q": query,
            "maxResults": min(max(max_results, 1), 50),
            "type": "video",
            "regionCode": self.region,
            "relevanceLanguage": self.language,
            "order": order,
            "key": self.api_key,
        }
        raw_search = self._get("search", params)
        video_ids = [
            item.get("id", {}).get("videoId")
            for item in raw_search.get("items", [])
            if item.get("id", {}).get("videoId")
        ]
        raw_videos = self.get_videos(video_ids) if video_ids else {"items": []}
        details_by_id = {item.get("id"): item for item in raw_videos.get("items", [])}
        results = self._normalize_search_results(raw_search, details_by_id)
        return {
            "query": query,
            "raw_search_response": raw_search,
            "raw_videos_response": raw_videos,
            "results": results,
        }

    def get_videos(self, video_ids: list[str]) -> dict[str, Any]:
        if not video_ids:
            return {"items": []}
        params = {
            "part": "snippet,contentDetails,statistics,status",
            "id": ",".join(video_ids[:50]),
            "key": self.api_key,
        }
        return self._get("videos", params)

    def video_metadata_by_id(self, video_ids: list[str]) -> dict[str, dict[str, Any]]:
        unique_ids = list(dict.fromkeys(video_id for video_id in video_ids if video_id))
        metadata: dict[str, dict[str, Any]] = {}

        for index in range(0, len(unique_ids), 50):
            batch = unique_ids[index : index + 50]
            raw_videos = self.get_videos(batch)
            for item in raw_videos.get("items", []):
                normalized = normalize_video_detail(item)
                video_id = normalized.get("video_id")
                if video_id:
                    metadata[video_id] = normalized

        return metadata

    def _get(self, resource: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = requests.get(
                f"{YOUTUBE_API_BASE}/{resource}",
                params=params,
                timeout=30,
            )
        except requests.RequestException as exc:
            # requests puts the full URL (including key=...) in its messages; never surface it.
            raise YouTubeApiError(
                f"YouTube API network error ({type(exc).__name__})"
            ) from None
        if response.ok:
            return response.json()

        try:
            payload = response.json()
            message = payload.get("error", {}).get("message", response.text)
        except ValueError:
            message = response.text
        raise YouTubeApiError(
            f"YouTube API request failed ({response.status_code}): {redact_secrets(message)[:300]}"
        )

    def _normalize_search_results(
        self,
        raw_search: dict[str, Any],
        details_by_id: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for position, item in enumerate(raw_search.get("items", []), start=1):
            raw_type = item.get("id", {}).get("kind", "").replace("youtube#", "")
            video_id = item.get("id", {}).get("videoId")
            detail = details_by_id.get(video_id, {})
            search_snippet = item.get("snippet", {})
            detail_snippet = detail.get("snippet", {})
            snippet = detail_snippet or search_snippet
            content_details = detail.get("contentDetails", {})
            statistics = detail.get("statistics", {})
            status = detail.get("status", {})
            duration_seconds = parse_iso8601_duration(content_details.get("duration"))

            result = {
                "position": position,
                "raw_position": position,
                "display_rank": None,
                "raw_type": raw_type,
                "live_broadcast_content": search_snippet.get("liveBroadcastContent"),
            }
            result.update(
                normalize_video_detail(
                    detail,
                    fallback_snippet=snippet,
                    fallback_video_id=video_id,
                )
            )
            label, confidence, evidence = classify_result(result)
            result.update(
                {
                    "label": label,
                    "label_confidence": confidence,
                    "evidence": evidence,
                }
            )
            results.append(result)
        return results


def normalize_video_detail(
    detail: dict[str, Any],
    fallback_snippet: dict[str, Any] | None = None,
    fallback_video_id: str | None = None,
) -> dict[str, Any]:
    fallback_snippet = fallback_snippet or {}
    snippet = detail.get("snippet", {}) or fallback_snippet
    content_details = detail.get("contentDetails", {})
    statistics = detail.get("statistics", {})
    status = detail.get("status", {})
    video_id = detail.get("id") or fallback_video_id
    duration_seconds = parse_iso8601_duration(content_details.get("duration"))

    return {
        "title": snippet.get("title"),
        "url": f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
        "video_id": video_id,
        "channel_name": snippet.get("channelTitle"),
        "channel_id": snippet.get("channelId"),
        "description": snippet.get("description"),
        "published_at": snippet.get("publishedAt"),
        "duration": content_details.get("duration"),
        "duration_seconds": duration_seconds,
        "view_count": parse_int(statistics.get("viewCount")),
        "like_count": parse_int(statistics.get("likeCount")),
        "comment_count": parse_int(statistics.get("commentCount")),
        "category_id": snippet.get("categoryId"),
        "tags": snippet.get("tags", []),
        "embeddable": status.get("embeddable"),
        "privacy_status": status.get("privacyStatus"),
        "metadata_source": "youtube_api" if detail else "youtube_search_snippet",
    }


def derive_query_from_video(video: dict[str, Any]) -> str:
    tags = [tag for tag in video.get("tags", []) if isinstance(tag, str)]
    if tags:
        return " ".join(tags[:5])[:120]

    text = f"{video.get('title') or ''} {video.get('description') or ''}"
    keywords: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[A-Za-z][A-Za-z0-9']{2,}", text.lower()):
        if token in STOP_WORDS or token in seen:
            continue
        seen.add(token)
        keywords.append(token)
        if len(keywords) >= 6:
            break

    if keywords:
        return " ".join(keywords)[:120]

    channel = (video.get("channel_name") or "").strip()
    return channel[:120]
