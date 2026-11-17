from __future__ import annotations

import json
from typing import Any

from openai import OpenAI

from .prompts import SYSTEM_PROMPT, build_query_user_message


class QueryGenerationError(RuntimeError):
    pass


GENERIC_QUERIES = {
    "latest trends",
    "popular videos",
    "trending videos",
    "viral videos",
    "youtube shorts",
    "popular music",
    "new music",
    "funny videos",
    "news",
}
MAX_QUERY_LENGTH = 80


def generate_queries(
    api_key: str,
    model: str,
    user_prompt: str,
    trend_terms: list[dict[str, Any]] | None = None,
    region: str = "IE",
    language: str = "en",
    query_count: int = 5,
    system_prompt: str | None = None,
) -> list[dict[str, Any]]:
    active_system_prompt = (system_prompt if system_prompt is not None else SYSTEM_PROMPT).strip()
    if not active_system_prompt:
        raise QueryGenerationError("The system prompt cannot be empty.")
    client = OpenAI(api_key=api_key)
    user_message = build_query_user_message(
        user_prompt=user_prompt,
        trend_terms=trend_terms,
        region=region,
        language=language,
        query_count=query_count,
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": active_system_prompt},
            {"role": "user", "content": user_message},
        ],
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content
    if not content:
        raise QueryGenerationError("The LLM returned an empty response.")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise QueryGenerationError("The LLM did not return valid JSON.") from exc
    return normalize_query_payload(payload, expected_count=query_count)


def normalize_query_payload(
    payload: dict[str, Any],
    expected_count: int = 5,
) -> list[dict[str, Any]]:
    raw_queries = payload.get("queries")
    if not isinstance(raw_queries, list):
        raise QueryGenerationError("The LLM response is missing a 'queries' list.")

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw_queries, start=1):
        if isinstance(item, str):
            query = item
            reason = ""
            rank = index
            source_terms: list[str] = []
        elif isinstance(item, dict):
            query = str(item.get("query", "")).strip()
            reason = str(item.get("reason", "")).strip()
            rank = int(item.get("rank") or index)
            source_terms = normalize_source_terms(item.get("source_terms"))
        else:
            continue

        query = " ".join(query.split())
        if not query or is_generic_query(query):
            continue
        query = query[:MAX_QUERY_LENGTH].strip()
        dedupe_key = query.casefold()
        if any(existing["query"].casefold() == dedupe_key for existing in normalized):
            continue
        normalized.append(
            {
                "rank": rank,
                "query": query,
                "reason": reason,
                "source_terms": source_terms,
            }
        )

    if len(normalized) != expected_count:
        raise QueryGenerationError(
            f"Expected exactly {expected_count} queries, got {len(normalized)}."
        )

    return normalized


def normalize_source_terms(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_terms = [value]
    elif isinstance(value, list):
        raw_terms = value
    else:
        raw_terms = []

    terms: list[str] = []
    for term in raw_terms:
        cleaned = " ".join(str(term).split())[:80]
        if cleaned and cleaned.casefold() not in {item.casefold() for item in terms}:
            terms.append(cleaned)
    return terms[:5]


def is_generic_query(query: str) -> bool:
    cleaned = " ".join(query.casefold().split())
    if cleaned in GENERIC_QUERIES:
        return True
    if cleaned.startswith("youtube ") and len(cleaned.split()) <= 3:
        return True
    return False
