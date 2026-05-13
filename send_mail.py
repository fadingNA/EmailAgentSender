#!/usr/bin/env python3
import argparse
import csv
import sys
import html
import json
import logging
import os
import re
import smtplib
import ssl
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.message import EmailMessage
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import SimpleNamespace

from component.app_vendors import collect_cached_app_vendors, format_vendor_context


def load_dotenv(path=".env"):
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_dotenv()

import ollama


SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", os.getenv("EMAIL", ""))
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", os.getenv("PASSWORD", ""))
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)
SMTP_TO = os.getenv("SMTP_TO", "")

MAIL_PROVIDER = os.getenv("MAIL_PROVIDER", "").lower()
MAILERSEND_API_KEY = os.getenv("MAILERSEND_API_KEY", "")
MAILERSEND_API_URL = os.getenv("MAILERSEND_API_URL", "https://api.mailersend.com/v1/email")
MAILERSEND_FROM = os.getenv("MAILERSEND_FROM", SMTP_FROM)
MAILERSEND_FROM_NAME = os.getenv("MAILERSEND_FROM_NAME", "")
MAILERSEND_TO = os.getenv("MAILERSEND_TO", SMTP_TO)
MAILERSEND_USER_AGENT = os.getenv("MAILERSEND_USER_AGENT", "openfang-news/1.0")

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:9b")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "")
OLLAMA_CHAT_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_CHAT_TIMEOUT_SECONDS", "180"))

DEFAULT_APP_VENDOR_DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_APP_VENDOR_CACHE_PATH = DEFAULT_APP_VENDOR_DATA_DIR / ".cache" / "app_vendors.json"
DIGEST_TOPIC = os.getenv("DIGEST_TOPIC", "latest cybersecurity news")
DIGEST_MAX_ITEMS = int(os.getenv("DIGEST_MAX_ITEMS", "12"))
GENERAL_SECURITY_MAX_ITEMS = int(os.getenv("GENERAL_SECURITY_MAX_ITEMS", "15"))
DIGEST_TIME = os.getenv("DIGEST_TIME", "08:00")
OLLAMA_TOOL_ITERATIONS = int(os.getenv("OLLAMA_TOOL_ITERATIONS", "6"))
ENABLE_TOOL_AGENT_FALLBACK = os.getenv("ENABLE_TOOL_AGENT_FALLBACK", "0").lower() in {"1", "true", "yes"}
APP_VENDOR_DATA_DIR = os.getenv("APP_VENDOR_DATA_DIR", str(DEFAULT_APP_VENDOR_DATA_DIR))
APP_VENDOR_COLUMN = os.getenv("APP_VENDOR_COLUMN", "app_vendor")
APP_VENDOR_LIMIT = int(os.getenv("APP_VENDOR_LIMIT", "50"))
APP_VENDOR_CACHE_PATH = os.getenv("APP_VENDOR_CACHE_PATH", str(DEFAULT_APP_VENDOR_CACHE_PATH))
WEB_SEARCH_RESULT_LIMIT = int(
    os.getenv(
        "WEB_SEARCH_RESULT_LIMIT",
        str(max(DIGEST_MAX_ITEMS, GENERAL_SECURITY_MAX_ITEMS + min(APP_VENDOR_LIMIT, 50))),
    )
)
WEB_SEARCH_RETRIES = int(os.getenv("WEB_SEARCH_RETRIES", "2"))
WEB_SEARCH_RETRY_DELAY_SECONDS = float(os.getenv("WEB_SEARCH_RETRY_DELAY_SECONDS", "2"))
WEB_SEARCH_ENABLED = os.getenv("WEB_SEARCH_ENABLED", "1").lower() in {"1", "true", "yes"}
WEB_SEARCH_STOP_TRACK_ON_EMPTY = os.getenv("WEB_SEARCH_STOP_TRACK_ON_EMPTY", "1").lower() in {"1", "true", "yes"}
VENDOR_SEARCH_QUERIES_PER_VENDOR = int(os.getenv("VENDOR_SEARCH_QUERIES_PER_VENDOR", "3"))
DEFAULT_CYBERSECURITY_FEED_URLS = [
    "https://www.bleepingcomputer.com/feed/",
    "https://feeds.feedburner.com/TheHackersNews",
    "https://www.securityweek.com/feed/",
    "https://www.cisa.gov/cybersecurity-advisories/all.xml",
]
CYBERSECURITY_FEED_URLS = [
    url.strip()
    for url in os.getenv(
        "CYBERSECURITY_FEED_URLS",
        ",".join(DEFAULT_CYBERSECURITY_FEED_URLS),
    ).split(",")
    if url.strip()
]
LOG_FILE = os.getenv("LOG_FILE", "logs/news_digest.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_TO_CONSOLE = os.getenv("LOG_TO_CONSOLE", "1").lower() not in {"0", "false", "no"}
APP_VENDOR_REFRESH_CACHE = False
APP_VENDOR_CONTEXT_CACHE = None


def setup_logger():
    log_path = Path(LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("openfang_news")
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if LOG_TO_CONSOLE:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    return logger


LOGGER = setup_logger()


class EmptyWebSearchResponse(RuntimeError):
    pass


def ollama_client():
    return ollama.Client(host=OLLAMA_HOST, timeout=OLLAMA_CHAT_TIMEOUT_SECONDS)


def env_list(name, default=""):
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def ollama_chat(messages):
    client = ollama_client()
    start = time.monotonic()
    LOGGER.info(
        "ollama_chat_start model=%r host=%r messages=%s",
        OLLAMA_MODEL,
        OLLAMA_HOST,
        len(messages),
    )
    response = client.chat(
        model=OLLAMA_MODEL,
        messages=messages,
        stream=False,
        options={"temperature": 0.2},
    )
    content = response["message"]["content"].strip()
    LOGGER.info(
        "ollama_chat_complete model=%r duration_seconds=%.1f chars=%s",
        OLLAMA_MODEL,
        time.monotonic() - start,
        len(content),
    )
    return content


def message_value(message, key, default=None):
    if isinstance(message, dict):
        return message.get(key, default)
    return getattr(message, key, default)


def tool_call_value(tool_call, key, default=None):
    if isinstance(tool_call, dict):
        return tool_call.get(key, default)
    return getattr(tool_call, key, default)


def function_value(function_call, key, default=None):
    if isinstance(function_call, dict):
        return function_call.get(key, default)
    return getattr(function_call, key, default)


def log_search_results(query, response):
    results = response.results
    for index, result in enumerate(results, start=1):
        LOGGER.info(
            "web_search_result query=%r rank=%s title=%r url=%r",
            query,
            index,
            result.title,
            result.url,
        )


def web_search(query: str, max_results: int = 5):
    """Search the web for current source material.

    Args:
        query: Search query string.
        max_results: Maximum number of search results to return.
    """
    if not OLLAMA_API_KEY:
        raise RuntimeError("Set OLLAMA_API_KEY for Ollama web search.")

    payload = json.dumps(
        {"query": query, "max_results": min(int(max_results), 10)}
    ).encode("utf-8")
    data = None
    max_attempts = WEB_SEARCH_RETRIES + 1

    for attempt in range(1, max_attempts + 1):
        LOGGER.info(
            "web_search_start query=%r max_results=%s attempt=%s max_attempts=%s",
            query,
            max_results,
            attempt,
            max_attempts,
        )
        request = urllib.request.Request(
            "https://ollama.com/api/web_search",
            data=payload,
            headers={
                "Authorization": f"Bearer {OLLAMA_API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as http_response:
                response_body = http_response.read().decode("utf-8", errors="replace")
                LOGGER.info(
                    "web_search_response query=%r status=%s bytes=%s",
                    query,
                    http_response.status,
                    len(response_body),
                )
        except urllib.error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            LOGGER.warning(
                "web_search_http_error query=%r status=%s body_preview=%r",
                query,
                exc.code,
                response_body[:500],
            )
            if exc.code in {429, 500, 502, 503, 504} and attempt < max_attempts:
                time.sleep(WEB_SEARCH_RETRY_DELAY_SECONDS * attempt)
                continue
            raise

        if not response_body.strip():
            LOGGER.warning("web_search_empty_response query=%r attempt=%s", query, attempt)
            if attempt < max_attempts:
                time.sleep(WEB_SEARCH_RETRY_DELAY_SECONDS * attempt)
                continue
            raise EmptyWebSearchResponse(f"Ollama web_search returned an empty response for query: {query}")

        try:
            data = json.loads(response_body)
            break
        except json.JSONDecodeError as exc:
            LOGGER.warning(
                "web_search_invalid_json query=%r attempt=%s body_preview=%r error=%s",
                query,
                attempt,
                response_body[:500],
                exc,
            )
            if attempt < max_attempts:
                time.sleep(WEB_SEARCH_RETRY_DELAY_SECONDS * attempt)
                continue
            raise RuntimeError(
                f"Ollama web_search returned invalid JSON for query {query!r}: {exc}"
            ) from exc

    if data is None:
        raise RuntimeError(f"Ollama web_search did not return data for query: {query}")

    response = SimpleNamespace(
        results=[
            SimpleNamespace(
                title=item.get("title", ""),
                url=item.get("url", ""),
                content=item.get("content", ""),
            )
            for item in data.get("results", [])
        ]
    )
    log_search_results(query, response)
    LOGGER.info("web_search_complete query=%r results=%s", query, len(response.results))
    return response


def get_app_vendor_context():
    global APP_VENDOR_REFRESH_CACHE, APP_VENDOR_CONTEXT_CACHE

    if APP_VENDOR_CONTEXT_CACHE is not None and not APP_VENDOR_REFRESH_CACHE:
        return APP_VENDOR_CONTEXT_CACHE

    try:
        vendors = collect_cached_app_vendors(
            data_dir=Path(APP_VENDOR_DATA_DIR),
            column_name=APP_VENDOR_COLUMN,
            limit=APP_VENDOR_LIMIT,
            cache_path=Path(APP_VENDOR_CACHE_PATH),
            refresh_cache=APP_VENDOR_REFRESH_CACHE,
        )
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        LOGGER.warning("app_vendor_context_failed error=%s", exc)
        vendors = []
    APP_VENDOR_REFRESH_CACHE = False
    APP_VENDOR_CONTEXT_CACHE = vendors, format_vendor_context(vendors)
    LOGGER.info("app_vendor_context_count count=%s", len(vendors))
    return APP_VENDOR_CONTEXT_CACHE


def digest_date_window():
    today = datetime.now()
    yesterday = today - timedelta(days=1)
    return (
        today.strftime("%B %d, %Y"),
        yesterday.strftime("%B %d, %Y"),
    )


def digest_format_instructions(has_app_vendors=False):
    vendor_section = (
        "## 2. Targeted Vendor Watch - All Relevant Matches\n"
        "Cover the targeted vendors supplied below. Include every relevant current "
        "match you found for those vendors without a top-N display limit. Group by "
        "vendor when possible. If a targeted vendor has no current finding in the "
        "available sources, omit that vendor instead of inventing a finding.\n\n"
        if has_app_vendors
        else "## 2. Targeted Vendor Watch - All Relevant Matches\nNo targeted vendor list was available.\n\n"
    )
    return (
        "Use this exact markdown structure:\n\n"
        "# Cybersecurity Briefing\n"
        "## Executive Snapshot\n"
        "Write 3-5 bullets with the most important takeaways.\n\n"
        f"## 1. General Company Security - Top {GENERAL_SECURITY_MAX_ITEMS} (Today & Yesterday)\n"
        f"List up to {GENERAL_SECURITY_MAX_ITEMS} important general cybersecurity stories from today and yesterday. "
        "Use a numbered list. Each item must name the company, product, or organization, explain why it matters, "
        "and include a source link. For vulnerability or patch stories, include granular technical details: "
        "affected product/component, CVE IDs when available, CVSS score/severity when available, vulnerability "
        "class, attack vector, exploitation status, impact, fixed version or mitigation, and patch priority. "
        "If a story includes multiple critical CVEs, include a markdown table with one CVE or one affected "
        "product per row instead of compressing details into prose.\n\n"
        f"{vendor_section}"
        "For every vendor finding, provide the most granular details the sources support: affected product, "
        "affected versions if stated, CVE ID, CVSS score, vulnerability type, impact, exploitation status, "
        "patched version or workaround, and an operational defender action. Use `Not stated` instead of "
        "guessing when a detail is unavailable.\n\n"
        "Markdown table rules:\n"
        "- Put the header row, separator row, and every data row on separate lines.\n"
        "- Do not put a complete table on one line.\n"
        "- Use standard pipe-table syntax only, for example `| Product | CVE ID | CVSS | Impact | Action |`.\n"
        "- Keep table cells concise so the email renderer can display them cleanly.\n\n"
        "## Defensive Actions\n"
        "Write concise recommended actions for defenders. Group actions by priority and mention the product "
        "or CVE each action addresses when possible.\n\n"
        "## References\n"
        "List every source title and URL used."
    )


def run_ollama_web_search_agent():
    LOGGER.info(
        "digest_tool_agent_start model=%r topic=%r max_items=%s",
        OLLAMA_MODEL,
        DIGEST_TOPIC,
        DIGEST_MAX_ITEMS,
    )
    client = ollama_client()
    today, yesterday = digest_date_window()
    app_vendors, app_vendor_context = get_app_vendor_context()
    tools = [web_search]
    available_tools = {
        "web_search": web_search,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You are a cybersecurity news analyst. Use only the available "
                "web_search tool to decide your own search queries for current "
                "news. Do not rely on a fixed feed list. Use only web_search "
                "results as factual sources. Include "
                "source links for every important claim in the final digest and "
                "finish with a References section listing the title and URL of each "
                "source used. Focus on events from today and yesterday."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Today is {today}. Yesterday was {yesterday}. Create a daily "
                f"briefing about {DIGEST_TOPIC}. Search for two groups: "
                f"1. the top {GENERAL_SECURITY_MAX_ITEMS} general company security stories "
                "from today and yesterday; 2. targeted vendor cybersecurity findings "
                "for the supplied app vendors with no artificial display limit. "
                "Use multiple useful queries if needed.\n\n"
                f"{digest_format_instructions(bool(app_vendors))}"
                + (
                    "\n\nAdditional vendor focus: Search for recent cybersecurity "
                    "news, vulnerabilities, breaches, advisories, and threat activity "
                    f"related to these non-duplicated app vendors: {app_vendor_context}. "
                    "Prioritize vendor-specific findings when they are current and relevant."
                    if app_vendors
                    else ""
                )
            ),
        },
    ]

    for iteration in range(1, OLLAMA_TOOL_ITERATIONS + 1):
        LOGGER.info(
            "tool_agent_iteration_start iteration=%s max_iterations=%s messages=%s",
            iteration,
            OLLAMA_TOOL_ITERATIONS,
            len(messages),
        )
        start = time.monotonic()
        response = client.chat(
            model=OLLAMA_MODEL,
            messages=messages,
            tools=tools,
            stream=False,
            options={"temperature": 0.2, "num_ctx": 32768},
        )
        LOGGER.info(
            "tool_agent_iteration_complete iteration=%s duration_seconds=%.1f",
            iteration,
            time.monotonic() - start,
        )
        message = response["message"]
        messages.append(message)

        tool_calls = message_value(message, "tool_calls", []) or []
        if not tool_calls:
            content = message_value(message, "content", "")
            if content:
                LOGGER.info("tool_agent_final chars=%s", len(content.strip()))
                return content.strip()
            break

        for tool_call in tool_calls:
            function_call = tool_call_value(tool_call, "function", {})
            name = function_value(function_call, "name")
            args = function_value(function_call, "arguments", {}) or {}
            tool = available_tools.get(name)
            LOGGER.info("tool_call name=%r args=%r", name, args)
            if tool is None:
                result = f"Unknown tool: {name}"
            else:
                if name == "web_search":
                    args["max_results"] = min(int(args.get("max_results", 5)), 10)
                result = tool(**args)
            messages.append(
                {
                    "role": "tool",
                    "tool_name": name,
                    "content": str(result)[:8000],
                }
            )

    raise RuntimeError("Ollama web-search agent did not produce a final digest.")


def generate_general_search_queries():
    today, yesterday = digest_date_window()
    date_terms = f"{today} OR {yesterday}"
    queries = [
        f"{DIGEST_TOPIC} {date_terms}",
        f"cybersecurity vulnerabilities exploited in the wild {date_terms}",
        f"security patch advisory CVE critical {date_terms}",
        f"ransomware breach data leak cyberattack {date_terms}",
        f"CISA KEV exploited vulnerability {date_terms}",
        f"Microsoft Adobe SAP Cisco VMware security update {date_terms}",
    ]
    query_limit = min(len(queries), max(5, GENERAL_SECURITY_MAX_ITEMS // 2))
    for query in queries[:query_limit]:
        LOGGER.info("general_search_query query=%r", query)
    return queries[:query_limit]


def generate_vendor_search_queries(vendors):
    today, yesterday = digest_date_window()
    queries = []
    for vendor in vendors:
        vendor_name = str(vendor).strip()
        if not vendor_name:
            continue
        vendor_queries = [
            f"{vendor_name} security advisory CVE {today} {yesterday}",
            f"{vendor_name} vulnerability patch exploit {today} {yesterday}",
            f"{vendor_name} cyberattack breach ransomware {today} {yesterday}",
        ]
        queries.extend(vendor_queries[:VENDOR_SEARCH_QUERIES_PER_VENDOR])
    deduped_queries = list(dict.fromkeys(queries))
    for query in deduped_queries:
        LOGGER.info("vendor_search_query query=%r", query)
    return deduped_queries


def collect_ollama_web_search_results(queries, source_group):
    if not WEB_SEARCH_ENABLED:
        LOGGER.info("search_track_skipped group=%s reason=web_search_disabled", source_group)
        return []

    seen = set()
    results = []
    per_query = max(1, min(10, WEB_SEARCH_RESULT_LIMIT // max(1, len(queries)) + 2))
    LOGGER.info(
        "search_track_start group=%s queries=%s per_query=%s",
        source_group,
        len(queries),
        per_query,
    )
    for query in queries:
        try:
            response = web_search(query=query, max_results=per_query)
        except EmptyWebSearchResponse as exc:
            LOGGER.warning(
                "query_web_search_empty group=%s query=%r error=%s",
                source_group,
                query,
                exc,
            )
            if WEB_SEARCH_STOP_TRACK_ON_EMPTY:
                LOGGER.warning("search_track_aborted group=%s reason=empty_web_search_response", source_group)
                break
            continue
        except (RuntimeError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            LOGGER.warning(
                "query_web_search_failed group=%s query=%r error=%s",
                source_group,
                query,
                exc,
            )
            continue
        for result in response.results:
            url = result.url
            if url in seen:
                continue
            seen.add(url)
            results.append(
                {
                    "group": source_group,
                    "query": query,
                    "title": result.title,
                    "url": url,
                    "content": result.content,
                }
            )
            if len(results) >= WEB_SEARCH_RESULT_LIMIT:
                LOGGER.info("search_track_complete group=%s results=%s", source_group, len(results))
                return results
    if not results:
        LOGGER.warning("search_track_empty group=%s", source_group)
        return []
    LOGGER.info("search_track_complete group=%s results=%s", source_group, len(results))
    return results


def text_from_xml(element, *names):
    for name in names:
        found = element.find(name)
        if found is not None and found.text:
            return found.text.strip()
    for child in element:
        local_name = child.tag.rsplit("}", 1)[-1]
        if local_name in names and child.text:
            return child.text.strip()
    return ""


def link_from_feed_item(item):
    link = text_from_xml(item, "link")
    if link:
        return link
    for child in item:
        local_name = child.tag.rsplit("}", 1)[-1]
        if local_name == "link":
            href = child.attrib.get("href")
            if href:
                return href
    return ""


def strip_html_tags(value):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(value or ""))).strip()


def parse_feed_entries(feed_text, feed_url):
    root = ET.fromstring(feed_text)
    entries = root.findall(".//item")
    if not entries:
        entries = [
            element
            for element in root.findall(".//*")
            if element.tag.rsplit("}", 1)[-1] == "entry"
        ]

    parsed_entries = []
    for entry in entries:
        title = strip_html_tags(text_from_xml(entry, "title"))
        url = link_from_feed_item(entry)
        summary = strip_html_tags(
            text_from_xml(entry, "description", "summary", "content", "encoded")
        )
        published = text_from_xml(entry, "pubDate", "published", "updated")
        if not title or not url:
            continue
        parsed_entries.append(
            {
                "title": title,
                "url": url,
                "content": summary,
                "published": published,
                "query": feed_url,
            }
        )
    return parsed_entries


def fetch_feed_entries(feed_url):
    LOGGER.info("feed_fetch_start url=%r", feed_url)
    request = urllib.request.Request(
        feed_url,
        headers={"User-Agent": MAILERSEND_USER_AGENT},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        body = response.read().decode("utf-8", errors="replace")
    LOGGER.info("feed_fetch_response url=%r status=%s bytes=%s", feed_url, response.status, len(body))
    entries = parse_feed_entries(body, feed_url)
    LOGGER.info("feed_fetch_complete url=%r entries=%s", feed_url, len(entries))
    return entries


def collect_curated_feed_results(vendors):
    vendor_terms = [str(vendor).casefold() for vendor in vendors if str(vendor).strip()]
    seen = set()
    general_results = []
    vendor_results = []

    LOGGER.info(
        "curated_feed_search_start feeds=%s vendor_count=%s",
        len(CYBERSECURITY_FEED_URLS),
        len(vendor_terms),
    )
    for feed_url in CYBERSECURITY_FEED_URLS:
        try:
            entries = fetch_feed_entries(feed_url)
        except (ET.ParseError, OSError, UnicodeDecodeError, urllib.error.URLError, TimeoutError) as exc:
            LOGGER.warning("feed_fetch_failed url=%r error=%s", feed_url, exc)
            continue

        for entry in entries:
            url = entry["url"]
            if url in seen:
                continue
            seen.add(url)
            searchable_text = f"{entry['title']} {entry['content']}".casefold()
            result = {
                "query": entry["query"],
                "title": entry["title"],
                "url": url,
                "content": " ".join(
                    part for part in [entry.get("published"), entry["content"]] if part
                ),
            }
            if len(general_results) < GENERAL_SECURITY_MAX_ITEMS:
                general_results.append({"group": "general", **result})
            if vendor_terms and any(vendor in searchable_text for vendor in vendor_terms):
                vendor_results.append({"group": "vendor", **result})

            if (
                len(general_results) >= GENERAL_SECURITY_MAX_ITEMS
                and len(vendor_results) >= WEB_SEARCH_RESULT_LIMIT
            ):
                break

    results = general_results + vendor_results
    LOGGER.info(
        "curated_feed_search_complete general_results=%s vendor_results=%s total_results=%s",
        len(general_results),
        len(vendor_results),
        len(results),
    )
    return results


def summarize_search_results(results):
    if not results:
        return "No recent cybersecurity articles were found today."

    groups = {}
    for result in results:
        group = result.get("group", "general")
        groups[group] = groups.get(group, 0) + 1
    LOGGER.info("summarize_start results=%s groups=%s", len(results), groups)

    lines = []
    for index, result in enumerate(results, start=1):
        lines.append(
            "\n".join(
                [
                    f"{index}. {result['title']}",
                    f"Result group: {result.get('group', 'general')}",
                    f"Search query: {result['query']}",
                    f"URL: {result['url']}",
                    f"Snippet: {result['content']}",
                ]
            )
        )

    today, yesterday = digest_date_window()
    app_vendors, _ = get_app_vendor_context()
    digest = ollama_chat(
        [
            {
                "role": "system",
                "content": (
                    "You are a cybersecurity news analyst. Use only the provided "
                    "Ollama web_search results as factual sources. Include source "
                    "links for every important claim. Do not invent details not "
                    "present in the results. Finish with a References section that "
                    "lists every source title and URL used."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Today is {today}. Yesterday was {yesterday}. Create a daily briefing about {DIGEST_TOPIC}.\n\n"
                    "The search results are already separated into two groups: `general` for broad cybersecurity "
                    "news and `vendor` for findings based on the local app vendor data. Preserve both jobs in "
                    "one combined email: use general results for section 1 and vendor results for section 2. "
                    "If one group has no useful results, say that plainly in that section and still produce the "
                    "other section.\n\n"
                    f"{chr(10).join(lines)}\n\n"
                    f"{digest_format_instructions(bool(app_vendors))}"
                ),
            },
        ]
    )
    LOGGER.info("summarize_complete chars=%s", len(digest))
    return digest


def run_ollama_query_planner_search():
    app_vendors, _ = get_app_vendor_context()
    LOGGER.info(
        "two_track_search_start topic=%r general_max_items=%s vendor_count=%s",
        DIGEST_TOPIC,
        GENERAL_SECURITY_MAX_ITEMS,
        len(app_vendors),
    )
    general_queries = generate_general_search_queries()
    general_results = collect_ollama_web_search_results(general_queries, "general")

    vendor_results = []
    if app_vendors:
        vendor_queries = generate_vendor_search_queries(app_vendors)
        vendor_results = collect_ollama_web_search_results(vendor_queries, "vendor")
    else:
        LOGGER.info("vendor_search_skipped reason=no_app_vendors")

    results = general_results + vendor_results
    LOGGER.info(
        "two_track_search_complete general_results=%s vendor_results=%s total_results=%s",
        len(general_results),
        len(vendor_results),
        len(results),
    )
    if not results:
        LOGGER.warning("two_track_web_search_empty action=curated_feed_fallback")
        results = collect_curated_feed_results(app_vendors)
    if not results:
        raise RuntimeError("No web search results were collected from Ollama web search or curated feeds.")
    return summarize_search_results(results)


def fallback_digest(ollama_error):
    LOGGER.error("digest_failed error=%s", ollama_error)
    today = datetime.now().strftime("%B %d, %Y")
    lines = [
        f"Daily Cybersecurity News Digest - {today}",
        "",
        f"Cybersecurity digest source collection was unavailable: {ollama_error}",
        "",
        "Check the terminal logs for Ollama web_search empty responses, curated feed fetch failures, or local model timeout errors.",
    ]
    return "\n".join(lines)


def render_inline_markdown(text):
    escaped = html.escape(text)

    def replace_link(match):
        label = match.group(1)
        url = html.unescape(match.group(2))
        if not url.startswith(("http://", "https://", "mailto:")):
            return match.group(0)
        return (
            f'<a href="{html.escape(url, quote=True)}" '
            'style="color:#155eef;text-decoration:none;font-weight:700">'
            f"{label}</a>"
        )

    escaped = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", replace_link, escaped)
    escaped = re.sub(
        r"`([^`]+)`",
        r'<code style="font-family:Menlo,Consolas,monospace;font-size:13px;background:#edf2f7;color:#243041;padding:2px 5px;border-radius:4px">\1</code>',
        escaped,
    )
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", escaped)
    escaped = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"<em>\1</em>", escaped)
    return escaped


def markdown_to_email_html(markdown):
    blocks = []
    list_stack = []
    in_code_block = False
    code_lines = []
    paragraph_lines = []

    def split_table_row(line):
        cells = line.strip().strip("|").split("|")
        return [cell.strip() for cell in cells]

    def is_table_separator(line):
        cells = split_table_row(line)
        if not cells:
            return False
        return all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)

    def is_table_start(lines, index):
        if index + 1 >= len(lines):
            return False
        return "|" in lines[index] and is_table_separator(lines[index + 1])

    def render_table(headers, rows):
        header_html = "".join(
            '<th style="padding:11px 12px;background:#e8f0f8;color:#0f172a;'
            'font-size:13px;line-height:1.35;text-align:left;border:1px solid #cbd5e1;'
            'font-weight:800">'
            f"{render_inline_markdown(header)}</th>"
            for header in headers
        )
        row_html = []
        for row in rows:
            padded_row = row[: len(headers)] + [""] * max(0, len(headers) - len(row))
            cells = "".join(
                '<td style="padding:10px 12px;color:#334155;font-size:14px;'
                'line-height:1.45;border:1px solid #dbe5ef;vertical-align:top">'
                f"{render_inline_markdown(cell)}</td>"
                for cell in padded_row
            )
            row_html.append(f"<tr>{cells}</tr>")
        return (
            '<table style="width:100%;margin:0 0 18px;border-collapse:collapse;'
            'background:#ffffff;border:1px solid #cbd5e1">'
            f"<thead><tr>{header_html}</tr></thead>"
            f"<tbody>{''.join(row_html)}</tbody></table>"
        )

    def close_paragraph():
        if not paragraph_lines:
            return
        text = " ".join(line.strip() for line in paragraph_lines)
        blocks.append(
            '<p style="margin:0 0 16px;line-height:1.62;color:#334155;font-size:15px">'
            f"{render_inline_markdown(text)}</p>"
        )
        paragraph_lines.clear()

    def close_lists(target_depth=0):
        while len(list_stack) > target_depth:
            tag = list_stack.pop()
            blocks.append(f"</{tag}>")

    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            close_paragraph()
            close_lists()
            if in_code_block:
                blocks.append(
                    '<pre style="margin:0 0 16px;padding:14px 16px;background:#111827;'
                    'color:#f9fafb;border-radius:8px;overflow:auto;white-space:pre-wrap;'
                    'font-family:Menlo,Consolas,monospace;font-size:13px;line-height:1.5">'
                    f"{html.escape(chr(10).join(code_lines))}</pre>"
                )
                code_lines.clear()
                in_code_block = False
            else:
                in_code_block = True
            index += 1
            continue

        if in_code_block:
            code_lines.append(line)
            index += 1
            continue

        if not stripped:
            close_paragraph()
            close_lists()
            index += 1
            continue

        if is_table_start(lines, index):
            close_paragraph()
            close_lists()
            headers = split_table_row(lines[index])
            index += 2
            rows = []
            while index < len(lines):
                row_line = lines[index].strip()
                if not row_line or "|" not in row_line:
                    break
                rows.append(split_table_row(row_line))
                index += 1
            blocks.append(render_table(headers, rows))
            continue

        heading = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading:
            close_paragraph()
            close_lists()
            level = len(heading.group(1))
            heading_text = render_inline_markdown(heading.group(2).strip())
            if level == 1:
                blocks.append(
                    '<h1 style="margin:0 0 18px;font-size:25px;line-height:1.22;'
                    'font-weight:800;color:#0f172a">'
                    f"{heading_text}</h1>"
                )
            elif level == 2:
                blocks.append(
                    '<div style="margin:26px 0 14px;padding:13px 16px;'
                    'background:#f7fafc;border:1px solid #dbe5ef;border-left:4px solid #155eef;'
                    'border-radius:8px">'
                    '<h2 style="margin:0;font-size:18px;line-height:1.3;font-weight:800;color:#0f172a">'
                    f"{heading_text}</h2></div>"
                )
            else:
                blocks.append(
                    '<h3 style="margin:20px 0 10px;font-size:16px;line-height:1.3;'
                    'font-weight:800;color:#1e293b">'
                    f"{heading_text}</h3>"
                )
            index += 1
            continue

        list_item = re.match(r"^(\s*)([-*]|\d+[.])\s+(.+)$", line)
        if list_item:
            close_paragraph()
            indent = len(list_item.group(1).replace("\t", "    "))
            depth = indent // 2
            tag = "ol" if list_item.group(2).endswith(".") else "ul"
            while len(list_stack) > depth:
                blocks.append(f"</{list_stack.pop()}>")
            if len(list_stack) == depth or list_stack[-1] != tag:
                if len(list_stack) > depth:
                    blocks.append(f"</{list_stack.pop()}>")
                list_style = (
                    "margin:0 0 18px;padding:0;line-height:1.55;color:#334155;list-style-position:inside"
                    if depth == 0
                    else "margin:8px 0 14px 18px;padding:0;line-height:1.55;color:#334155"
                )
                blocks.append(
                    f'<{tag} style="{list_style}">'
                )
                list_stack.append(tag)
            item_style = (
                "margin:0 0 10px;padding:12px 14px;background:#ffffff;"
                "border:1px solid #e2e8f0;border-radius:8px;color:#334155"
                if depth == 0
                else "margin:0 0 8px;color:#334155"
            )
            blocks.append(
                f'<li style="{item_style}">'
                f"{render_inline_markdown(list_item.group(3).strip())}</li>"
            )
            index += 1
            continue

        close_lists()
        paragraph_lines.append(line)
        index += 1

    close_paragraph()
    close_lists()
    if in_code_block:
        blocks.append(
            '<pre style="margin:0 0 16px;padding:14px 16px;background:#111827;'
            'color:#f9fafb;border-radius:8px;overflow:auto;white-space:pre-wrap;'
            'font-family:Menlo,Consolas,monospace;font-size:13px;line-height:1.5">'
            f"{html.escape(chr(10).join(code_lines))}</pre>"
        )
    return "\n".join(blocks)


def build_email_html(subject, body):
    content = markdown_to_email_html(body)
    escaped_subject = html.escape(subject)
    generated_at = html.escape(datetime.now().strftime("%B %d, %Y"))
    return f"""<!doctype html>
<html>
  <body style="margin:0;padding:0;background:#edf2f7;font-family:Arial,Helvetica,sans-serif;color:#243041">
    <div style="display:none;max-height:0;overflow:hidden;color:#edf2f7;opacity:0">
      {escaped_subject}
    </div>
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;background:#edf2f7">
      <tr>
        <td align="center" style="padding:30px 14px">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;max-width:820px;background:#ffffff;border:1px solid #d8e2ec;border-radius:8px;overflow:hidden;box-shadow:0 14px 38px rgba(15,23,42,0.08)">
            <tr>
              <td style="padding:26px 30px;background:#111827;color:#ffffff;border-bottom:4px solid #2dd4bf">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse">
                  <tr>
                    <td style="vertical-align:top">
                      <div style="font-size:12px;letter-spacing:0.08em;text-transform:uppercase;color:#8bd3ff;font-weight:800">OpenFang News</div>
                      <h1 style="margin:8px 0 0;font-size:26px;line-height:1.22;font-weight:800;color:#ffffff">{escaped_subject}</h1>
                    </td>
                    <td align="right" style="vertical-align:top;white-space:nowrap">
                      <span style="display:inline-block;padding:7px 10px;border:1px solid rgba(255,255,255,0.24);border-radius:8px;color:#dbeafe;font-size:12px;font-weight:700">{generated_at}</span>
                    </td>
                  </tr>
                </table>
                <p style="margin:14px 0 0;color:#cbd5e1;font-size:14px;line-height:1.55">
                  Top company security stories from today and yesterday, plus targeted vendor intelligence from your app inventory.
                </p>
              </td>
            </tr>
            <tr>
              <td style="padding:30px;background:#fbfdff">
                {content}
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""


def make_email(subject, body, recipients, sender):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    message.add_alternative(build_email_html(subject, body), subtype="html")
    return message


def make_mailersend_email(subject, body, recipients, sender, sender_name=""):
    from_address = {"email": sender}
    if sender_name:
        from_address["name"] = sender_name

    return {
        "from": from_address,
        "to": [{"email": recipient} for recipient in recipients],
        "subject": subject,
        "text": body,
        "html": build_email_html(subject, body),
    }


def send_mailersend_email(subject, body):
    recipients = env_list("MAILERSEND_TO", MAILERSEND_TO)
    if not recipients:
        raise RuntimeError("Set MAILERSEND_TO or SMTP_TO to one or more comma-separated recipient emails.")
    if not MAILERSEND_API_KEY:
        raise RuntimeError("Set MAILERSEND_API_KEY for MailerSend.")
    if not MAILERSEND_FROM:
        raise RuntimeError("Set MAILERSEND_FROM or SMTP_FROM for the MailerSend sender address.")

    for recipient in recipients:
        payload = make_mailersend_email(
            subject,
            body,
            [recipient],
            MAILERSEND_FROM,
            MAILERSEND_FROM_NAME,
        )
        request = urllib.request.Request(
            MAILERSEND_API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {MAILERSEND_API_KEY}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": MAILERSEND_USER_AGENT,
                "X-Requested-With": "XMLHttpRequest",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                if response.status not in (200, 202):
                    response_body = response.read().decode("utf-8", errors="replace")
                    raise RuntimeError(f"MailerSend returned HTTP {response.status}: {response_body}")
        except urllib.error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"MailerSend failed for {recipient} with HTTP {exc.code}: {response_body}"
            ) from exc

    return recipients


def send_smtp_email(subject, body):
    recipients = env_list("SMTP_TO", SMTP_TO)
    if not recipients:
        raise RuntimeError("Set SMTP_TO to one or more comma-separated recipient emails.")
    if not SMTP_USER or not SMTP_PASSWORD:
        raise RuntimeError("Set SMTP_USER and SMTP_PASSWORD for the SMTP account.")
    if not SMTP_FROM:
        raise RuntimeError("Set SMTP_FROM or SMTP_USER for the sender address.")

    message = make_email(subject, body, recipients, SMTP_FROM)

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls(context=context)
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(message)

    return recipients


def selected_mail_provider():
    provider = MAIL_PROVIDER or ("mailersend" if MAILERSEND_API_KEY else "smtp")
    if provider not in {"mailersend", "smtp"}:
        raise RuntimeError("Set MAIL_PROVIDER to either 'mailersend' or 'smtp'.")
    return provider


def send_email(body, subject=None):
    subject = subject or f"Daily Cybersecurity News - {datetime.now().strftime('%Y-%m-%d')}"
    provider = selected_mail_provider()
    if provider == "mailersend":
        return send_mailersend_email(subject, body)
    return send_smtp_email(subject, body)


def generate_digest():
    try:
        return run_ollama_query_planner_search()
    except (
        RuntimeError,
        ollama.RequestError,
        ollama.ResponseError,
        ConnectionError,
        TypeError,
        ValueError,
        urllib.error.URLError,
        TimeoutError,
    ) as search_exc:
        LOGGER.warning("two_track_search_failed error=%s", search_exc)
        if not ENABLE_TOOL_AGENT_FALLBACK:
            return fallback_digest(f"two-track web search failed: {search_exc}")
        try:
            return run_ollama_web_search_agent()
        except (
            RuntimeError,
            ollama.RequestError,
            ollama.ResponseError,
            ConnectionError,
            TypeError,
            ValueError,
            urllib.error.URLError,
            TimeoutError,
        ) as tool_exc:
            return fallback_digest(f"two-track web search failed: {search_exc}; tool calling failed: {tool_exc}")


def run_once(dry_run=False):
    LOGGER.info("run_start dry_run=%s", dry_run)
    body = generate_digest()
    if dry_run:
        LOGGER.info("run_complete dry_run=True")
        print(body)
        return
    recipients = send_email(body)
    LOGGER.info("run_complete dry_run=False recipients=%s", recipients)
    print(f"Sent cybersecurity digest to {', '.join(recipients)}")


def run_test_send_mail():
    provider = selected_mail_provider()
    LOGGER.info("test_send_mail_start provider=%s", provider)
    body = "\n".join(
        [
            "OpenFang News test email",
            "",
            f"Sent at: {datetime.now().isoformat(timespec='seconds')}",
            f"Provider: {provider}",
            "",
            "If you received this, the email sending path is configured correctly.",
        ]
    )
    recipients = send_email(body, "OpenFang News test email")
    LOGGER.info("test_send_mail_complete recipients=%s", recipients)
    print(f"Sent test email to {', '.join(recipients)}")


def seconds_until(target_hhmm):
    now = datetime.now()
    hour, minute = [int(part) for part in target_hhmm.split(":", 1)]
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1, int((target - now).total_seconds()))


def run_daemon(target_hhmm):
    print(f"Daily cybersecurity digest daemon started. Send time: {target_hhmm}")
    while True:
        wait_seconds = seconds_until(target_hhmm)
        print(f"Next send in {wait_seconds} seconds")
        time.sleep(wait_seconds)
        try:
            run_once(dry_run=False)
        except Exception as exc:
            print(f"Digest send failed: {exc}")
        time.sleep(60)


def print_cron(time_hhmm):
    hour, minute = [int(part) for part in time_hhmm.split(":", 1)]
    script_path = Path(__file__).resolve()
    python_path = "/usr/bin/env python3"
    print(f"{minute} {hour} * * * cd {script_path.parent} && {python_path} {script_path}")


def main():
    global APP_VENDOR_REFRESH_CACHE

    parser = argparse.ArgumentParser(
        description="Send a daily Ollama-generated cybersecurity news digest to multiple email recipients."
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the digest instead of emailing it.")
    parser.add_argument("--test-send-mail", action="store_true", help="Send a simple test email without fetching news.")
    parser.add_argument("--daemon", action="store_true", help="Keep running and send once per day.")
    parser.add_argument("--time", default=DIGEST_TIME, help="Daily send time in 24-hour HH:MM format.")
    parser.add_argument("--print-cron", action="store_true", help="Print a crontab line for daily automation.")
    parser.add_argument(
        "--refresh-app-vendors",
        action="store_true",
        help="Rebuild the cached app_vendor list from CSV files before generating the digest.",
    )
    args = parser.parse_args()
    APP_VENDOR_REFRESH_CACHE = args.refresh_app_vendors

    if args.print_cron:
        print_cron(args.time)
    elif args.test_send_mail:
        run_test_send_mail()
    elif args.daemon:
        run_daemon(args.time)
    else:
        run_once(dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
