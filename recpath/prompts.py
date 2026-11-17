SYSTEM_PROMPT = """You generate concrete YouTube search queries for a real-time video tracking tool.
Return exactly 5 queries.
Each query must be short, specific, in English, and directly usable in YouTube search.
Focus on Ireland (IE), English-language YouTube discovery, and the user's stated interest.
Prefer concrete entities, communities, events, slang, products, creators, genres, or timely topic phrases over broad categories.
Use selected Google Trends terms only as context; do not copy unrelated trend terms blindly.
Do not return generic queries such as "viral videos", "latest trends", "popular music", or "YouTube shorts".
Do not return long sentences, explanations, punctuation-heavy phrases, hashtags-only phrases, or more than 8 words per query.
Return strict JSON with this shape:
{
  "queries": [
    {
      "rank": 1,
      "query": "short specific query",
      "reason": "brief reason",
      "source_terms": ["trend or user term"]
    }
  ]
}
"""


def build_query_user_message(
    *,
    user_prompt: str,
    region: str = "IE",
    language: str = "en",
    trend_terms: list[dict[str, object]] | None = None,
    query_count: int = 5,
) -> str:
    trend_terms = trend_terms or []
    trend_lines = []
    for item in trend_terms[:15]:
        term = item.get("term")
        if not term:
            continue
        rank = item.get("rank")
        trend_type = item.get("trend_type")
        date = item.get("date")
        score = item.get("score")
        source = item.get("source")
        parts = [str(term)]
        if rank:
            parts.append(f"rank={rank}")
        if trend_type:
            parts.append(f"type={trend_type}")
        if date:
            parts.append(f"date={date}")
        if score:
            parts.append(f"score={score}")
        if source:
            parts.append(f"source={source}")
        trend_lines.append(" - " + "; ".join(parts))

    trend_block = "\n".join(trend_lines) if trend_lines else "No selected Google Trends context."
    return f"""User interest description:
{user_prompt.strip()}

Target region: {region}
Target language: {language}
Required number of queries: {query_count}

Selected Google Trends context:
{trend_block}

Task:
Generate exactly {query_count} YouTube search queries that a researcher can paste into YouTube immediately.
Make them specific enough to retrieve concrete videos, not generic topic buckets.
"""
