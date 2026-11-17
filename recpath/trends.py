from __future__ import annotations

import csv
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import requests


PUBLIC_TRENDS_DATASET = "bigquery-public-data.google_trends"
TRENDS_RSS_URL = "https://trends.google.com/trending/rss?geo={region}"
TRENDS_PAGE_URL = "https://trends.google.com/trending?geo={region}"
TRENDS_EXPLORE_URL = "https://trends.google.com/trends/explore"
PREFERRED_TABLE_IDS = (
    "international_top_rising_terms",
    "international_top_terms",
    "top_rising_terms",
    "top_terms",
)
TERM_FIELDS = ("term", "query", "search_term", "keyword")
DATE_FIELDS = ("week", "date", "refresh_date", "refresh_time", "day")
RANK_FIELDS = ("rank", "rank_position")
SCORE_FIELDS = ("score", "value", "search_volume", "percent_gain")
COUNTRY_CODE_FIELDS = ("country_code", "country")
COUNTRY_NAME_FIELDS = ("country_name",)
REGION_COUNTRY_NAMES = {
    "IE": "Ireland",
}
CSV_TERM_FIELDS = (
    "trends",
    "trend",
    "search term",
    "search terms",
    "query",
    "queries",
    "topic",
    "title",
)
CSV_SCORE_FIELDS = ("search volume", "volume", "score", "interest", "value", "growth")
CSV_DATE_FIELDS = ("started", "start time", "date", "published", "updated")
CSV_LINK_FIELDS = ("explore link", "link", "url")


class GoogleTrendsError(RuntimeError):
    pass


def build_google_trends_explore_url(
    seed_query: str,
    *,
    region: str = "IE",
    language: str = "en",
    date_range: str = "now 7-d",
    search_property: str = "youtube",
) -> str:
    seed_query = " ".join(seed_query.split())
    if not seed_query:
        return ""
    params = {
        "date": date_range,
        "geo": region.upper(),
        "gprop": search_property,
        "hl": language,
        "q": seed_query,
    }
    return f"{TRENDS_EXPLORE_URL}?{urlencode(params)}"


def fetch_google_trends_rss(
    *,
    region: str = "IE",
    limit: int = 25,
    timeout: int = 20,
    http_get: Any = None,
) -> dict[str, Any]:
    region = region.upper()
    source_url = TRENDS_RSS_URL.format(region=quote(region))
    getter = http_get or requests.get
    try:
        response = getter(
            source_url,
            timeout=timeout,
            headers={"User-Agent": "RecPath/1.0"},
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise GoogleTrendsError(f"Could not fetch the official Google Trends RSS feed: {exc}") from exc
    except Exception as exc:
        raise GoogleTrendsError(f"Could not fetch the official Google Trends RSS feed: {exc}") from exc

    raw_items = parse_google_trends_rss(response.content)
    terms = normalize_rss_items(raw_items, region=region, limit=limit)
    if not terms:
        raise GoogleTrendsError("The official Google Trends RSS feed returned no usable terms.")

    return {
        "source": "google_trends_rss",
        "source_label": "Google Trends Trending Now RSS",
        "source_url": source_url,
        "source_page_url": TRENDS_PAGE_URL.format(region=quote(region)),
        "region": region,
        "fetched_at": utc_now(),
        "raw_items": raw_items,
        "terms": terms,
        "processing_steps": [
            "Read each official RSS <item> in feed order.",
            "Use the item title as the trend term.",
            "Remove blank and case-insensitive duplicate terms.",
            f"Keep the first {min(max(limit, 1), 50)} terms.",
        ],
    }


def parse_google_trends_rss(content: bytes | str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise GoogleTrendsError("The Google Trends RSS response was not valid XML.") from exc

    items: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        fields = {local_name(child.tag): (child.text or "").strip() for child in item}
        items.append(
            {
                "title": fields.get("title", ""),
                "approx_traffic": fields.get("approx_traffic", ""),
                "published_at": fields.get("pubDate", ""),
                "description": fields.get("description", ""),
                "link": fields.get("link", ""),
            }
        )
    return items


def normalize_rss_items(
    items: list[dict[str, Any]],
    *,
    region: str,
    limit: int = 25,
) -> list[dict[str, Any]]:
    terms: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        term = clean_term(item.get("title"))
        key = term.casefold()
        if not term or key in seen:
            continue
        seen.add(key)
        terms.append(
            {
                "rank": len(terms) + 1,
                "term": term,
                "trend_type": "trending_now",
                "region": region.upper(),
                "source": "google_trends_rss",
                "source_table": None,
                "date": item.get("published_at") or None,
                "score": item.get("approx_traffic") or None,
                "url": item.get("link") or None,
            }
        )
        if len(terms) >= min(max(limit, 1), 50):
            break
    return terms


def parse_google_trends_csv(
    content: bytes | str,
    *,
    region: str = "IE",
    limit: int = 25,
    filename: str | None = None,
    seed_query: str | None = None,
    source_page_url: str | None = None,
) -> dict[str, Any]:
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content.lstrip("\ufeff")
    lines = text.splitlines()
    header_index, headers = find_csv_header(lines)
    if header_index is None:
        raise GoogleTrendsError(
            "Could not find a trend/query column in the CSV. Export a related-queries "
            "or Trending Now CSV from Google Trends."
        )

    trend_type = detect_csv_trend_type(lines, header_index)
    reader = csv.DictReader(lines[header_index:])
    raw_items = [clean_csv_row(row) for row in reader if any((value or "").strip() for value in row.values())]
    terms = normalize_csv_items(
        raw_items,
        region=region,
        limit=limit,
        trend_type=trend_type,
    )
    if not terms:
        raise GoogleTrendsError("The uploaded CSV contained no usable trend terms.")

    return {
        "source": "google_trends_csv",
        "source_label": "Interest-based Google Trends Explore CSV",
        "source_url": None,
        "source_page_url": source_page_url or TRENDS_PAGE_URL.format(region=quote(region.upper())),
        "source_filename": filename,
        "seed_query": " ".join((seed_query or "").split()) or None,
        "search_property": "youtube" if source_page_url and "gprop=youtube" in source_page_url else None,
        "region": region.upper(),
        "fetched_at": utc_now(),
        "detected_header_row": header_index + 1,
        "detected_columns": headers,
        "raw_items": raw_items,
        "terms": terms,
        "processing_steps": [
            f"Detected the CSV header on row {header_index + 1}.",
            f"Classified the uploaded table as {trend_type} queries.",
            "Detected the trend term, volume/date, and link columns by their header names.",
            "Removed blank and case-insensitive duplicate terms.",
            f"Kept the first {min(max(limit, 1), 50)} terms in uploaded-file order.",
        ],
    }


def find_csv_header(lines: list[str]) -> tuple[int | None, list[str]]:
    aliases = {normalize_header(name) for name in CSV_TERM_FIELDS}
    for index, line in enumerate(lines[:20]):
        headers = next(csv.reader([line]), [])
        if any(normalize_header(header) in aliases for header in headers):
            return index, headers
    return None, []


def detect_csv_trend_type(lines: list[str], header_index: int) -> str:
    for line in reversed(lines[max(0, header_index - 5):header_index]):
        values = [value.strip().casefold() for value in next(csv.reader([line]), []) if value.strip()]
        if len(values) == 1 and values[0] in {"top", "rising"}:
            return values[0]
    return "related"


def normalize_csv_items(
    items: list[dict[str, Any]],
    *,
    region: str,
    limit: int = 25,
    trend_type: str = "related",
) -> list[dict[str, Any]]:
    terms: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        term = clean_term(value_for_alias(item, CSV_TERM_FIELDS))
        key = term.casefold()
        if not term or key in seen:
            continue
        seen.add(key)
        terms.append(
            {
                "rank": len(terms) + 1,
                "term": term,
                "trend_type": trend_type,
                "region": region.upper(),
                "source": "google_trends_csv",
                "source_table": None,
                "date": value_for_alias(item, CSV_DATE_FIELDS) or None,
                "score": value_for_alias(item, CSV_SCORE_FIELDS) or None,
                "url": value_for_alias(item, CSV_LINK_FIELDS) or None,
            }
        )
        if len(terms) >= min(max(limit, 1), 50):
            break
    return terms


def value_for_alias(item: dict[str, Any], aliases: tuple[str, ...]) -> str:
    normalized = {normalize_header(key): value for key, value in item.items()}
    for alias in aliases:
        value = normalized.get(normalize_header(alias))
        if value:
            return str(value).strip()
    return ""


def clean_csv_row(row: dict[str, Any]) -> dict[str, str]:
    return {
        str(key or "").strip(): str(value or "").strip()
        for key, value in row.items()
        if key is not None
    }


def normalize_header(value: str) -> str:
    return " ".join(str(value).strip().casefold().replace("_", " ").split())


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_google_trends_terms(
    *,
    credentials_path: str,
    project_id: str | None = None,
    region: str = "IE",
    limit: int = 25,
) -> dict[str, Any]:
    try:
        from google.cloud import bigquery
        from google.oauth2 import service_account
    except ImportError as exc:
        raise GoogleTrendsError(
            "Google Trends Assist requires `google-cloud-bigquery`. "
            "Install dependencies with `python -m pip install -r requirements.txt`."
        ) from exc

    if credentials_path.lstrip().startswith("{"):
        raise GoogleTrendsError(
            "GCP_CREDENTIALS_PATH must be the path of the service-account JSON file, "
            "not its content."
        )
    path = Path(credentials_path).expanduser()
    try:
        found = path.is_file()
    except OSError:
        found = False
    if not found:
        # Never echo the value: it may be key material set by mistake.
        looks_like_a_path = len(credentials_path) < 255 and not any(
            marker in credentials_path for marker in ("\n", "{", "PRIVATE KEY")
        )
        shown = f" ({path.name})" if looks_like_a_path else ""
        raise GoogleTrendsError(f"GCP credentials file was not found{shown}.")

    credentials = service_account.Credentials.from_service_account_file(str(path))
    billing_project = project_id or credentials.project_id
    if not billing_project:
        raise GoogleTrendsError(
            "A GCP billing project is required. Pass `--gcp-project-id`."
        )

    client = bigquery.Client(project=billing_project, credentials=credentials)
    table_ids = discover_google_trends_tables(client)
    errors: list[str] = []

    for table_id in preferred_table_order(table_ids):
        try:
            table = client.get_table(f"{PUBLIC_TRENDS_DATASET}.{table_id}")
            spec = infer_google_trends_schema([field.name for field in table.schema])
            if not spec:
                continue
            rows = query_google_trends_table(
                client=client,
                bigquery=bigquery,
                table_id=table_id,
                spec=spec,
                region=region,
                limit=limit,
            )
            terms = normalize_trend_rows(rows, table_id=table_id, region=region, limit=limit)
            if terms:
                return {
                    "source": "google_trends_bigquery",
                    "source_label": "Google Trends BigQuery public dataset",
                    "source_url": None,
                    "source_page_url": TRENDS_PAGE_URL.format(region=quote(region.upper())),
                    "dataset": PUBLIC_TRENDS_DATASET,
                    "table": table_id,
                    "region": region,
                    "fetched_at": utc_now(),
                    "terms": terms,
                    "raw_items": [row_to_dict(row) for row in rows],
                    "available_tables": table_ids,
                    "processing_steps": [
                        "Discovered available tables in the public Google Trends dataset.",
                        f"Selected the first compatible preferred table: {table_id}.",
                        f"Filtered rows to region {region.upper()} and the latest available date.",
                        "Removed blank and case-insensitive duplicate terms.",
                        f"Kept the first {min(max(limit, 1), 50)} terms.",
                    ],
                }
        except Exception as exc:
            errors.append(f"{table_id}: {exc}")

    detail = "; ".join(errors[:3]) if errors else "No compatible trend table was found."
    raise GoogleTrendsError(f"No Ireland Google Trends rows were available. {detail}")


def discover_google_trends_tables(client: Any) -> list[str]:
    try:
        tables = client.list_tables(PUBLIC_TRENDS_DATASET)
        return sorted(table.table_id for table in tables)
    except Exception as exc:
        raise GoogleTrendsError(
            f"Could not list Google Trends BigQuery tables: {exc}"
        ) from exc


def preferred_table_order(table_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for table_id in PREFERRED_TABLE_IDS:
        if table_id in table_ids:
            ordered.append(table_id)
            seen.add(table_id)
    for table_id in table_ids:
        if table_id not in seen and ("international" in table_id or "trend" in table_id):
            ordered.append(table_id)
            seen.add(table_id)
    return ordered


def infer_google_trends_schema(field_names: list[str]) -> dict[str, str] | None:
    fields = {field.lower(): field for field in field_names}
    term_col = first_field(fields, TERM_FIELDS)
    if not term_col:
        return None

    country_code_col = first_field(fields, COUNTRY_CODE_FIELDS)
    country_name_col = first_field(fields, COUNTRY_NAME_FIELDS)
    if not country_code_col and not country_name_col:
        return None

    return {
        "term": term_col,
        "country_code": country_code_col or "",
        "country_name": country_name_col or "",
        "date": first_field(fields, DATE_FIELDS) or "",
        "rank": first_field(fields, RANK_FIELDS) or "",
        "score": first_field(fields, SCORE_FIELDS) or "",
    }


def first_field(fields: dict[str, str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in fields:
            return fields[candidate]
    return None


def query_google_trends_table(
    *,
    client: Any,
    bigquery: Any,
    table_id: str,
    spec: dict[str, str],
    region: str,
    limit: int,
) -> list[Any]:
    region = region.upper()
    country_name = REGION_COUNTRY_NAMES.get(region, region)
    table_ref = f"{PUBLIC_TRENDS_DATASET}.{safe_identifier(table_id)}"
    term_col = safe_identifier(spec["term"])
    date_col = safe_identifier(spec["date"]) if spec.get("date") else ""
    rank_col = safe_identifier(spec["rank"]) if spec.get("rank") else ""
    score_col = safe_identifier(spec["score"]) if spec.get("score") else ""

    where_sql = country_filter_sql(spec)
    if not where_sql:
        return []

    select_parts = [
        f"CAST(`{term_col}` AS STRING) AS term",
        f"'{table_id}' AS source_table",
    ]
    if date_col:
        select_parts.append(f"CAST(`{date_col}` AS STRING) AS trend_date")
    else:
        select_parts.append("CAST(NULL AS STRING) AS trend_date")
    if rank_col:
        select_parts.append(f"SAFE_CAST(`{rank_col}` AS INT64) AS trend_rank")
    else:
        select_parts.append("CAST(NULL AS INT64) AS trend_rank")
    if score_col:
        select_parts.append(f"CAST(`{score_col}` AS STRING) AS trend_score")
    else:
        select_parts.append("CAST(NULL AS STRING) AS trend_score")

    latest_filter = ""
    if date_col:
        latest_filter = (
            f"AND `{date_col}` = ("
            f"SELECT MAX(`{date_col}`) FROM `{table_ref}` WHERE ({where_sql})"
            ")"
        )

    order_parts = []
    if date_col:
        order_parts.append(f"`{date_col}` DESC")
    if rank_col:
        order_parts.append(f"`{rank_col}` ASC")
    if score_col:
        order_parts.append(f"`{score_col}` DESC")
    order_sql = f"ORDER BY {', '.join(order_parts)}" if order_parts else "ORDER BY term"

    sql = f"""
        SELECT
          {", ".join(select_parts)}
        FROM `{table_ref}`
        WHERE ({where_sql})
          AND TRIM(CAST(`{term_col}` AS STRING)) != ''
          {latest_filter}
        {order_sql}
        LIMIT @limit
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("region", "STRING", region),
            bigquery.ScalarQueryParameter("country_name", "STRING", country_name.lower()),
            bigquery.ScalarQueryParameter("country_name_upper", "STRING", country_name.upper()),
            bigquery.ScalarQueryParameter("limit", "INT64", min(max(limit, 1), 50)),
        ]
    )
    return list(client.query(sql, job_config=job_config).result())


def country_filter_sql(spec: dict[str, str]) -> str:
    filters: list[str] = []
    country_code_col = spec.get("country_code")
    country_name_col = spec.get("country_name")
    if country_code_col:
        col = safe_identifier(country_code_col)
        filters.append(
            f"UPPER(CAST(`{col}` AS STRING)) IN (@region, @country_name_upper)"
        )
    if country_name_col:
        col = safe_identifier(country_name_col)
        filters.append(f"LOWER(CAST(`{col}` AS STRING)) = @country_name")
    return " OR ".join(filters)


def safe_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise GoogleTrendsError(f"Unsafe BigQuery identifier: {value}")
    return value


def normalize_trend_rows(
    rows: list[Any],
    *,
    table_id: str,
    region: str,
    limit: int = 25,
) -> list[dict[str, Any]]:
    terms: list[dict[str, Any]] = []
    seen: set[str] = set()
    trend_type = "rising" if "rising" in table_id else "top"

    for row in rows:
        item = dict(row.items()) if hasattr(row, "items") else dict(row)
        term = clean_term(item.get("term"))
        if not term:
            continue
        dedupe_key = term.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        terms.append(
            {
                "rank": item.get("trend_rank") or len(terms) + 1,
                "term": term,
                "trend_type": trend_type,
                "region": region,
                "source": "google_trends_bigquery",
                "source_table": item.get("source_table") or table_id,
                "date": item.get("trend_date"),
                "score": item.get("trend_score"),
            }
        )
        if len(terms) >= limit:
            break

    return terms


def clean_term(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())[:120]


def row_to_dict(row: Any) -> dict[str, Any]:
    return dict(row.items()) if hasattr(row, "items") else dict(row)


def selected_trend_terms_for_prompt(terms: list[dict[str, Any]], selected: list[str]) -> list[dict[str, Any]]:
    selected_keys = {term.casefold() for term in selected}
    return [term for term in terms if term.get("term", "").casefold() in selected_keys]
