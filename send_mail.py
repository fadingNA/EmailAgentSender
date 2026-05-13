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

DEFAULT_APP_VENDOR_DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_APP_VENDOR_CACHE_PATH = DEFAULT_APP_VENDOR_DATA_DIR / ".cache" / "app_vendors.json"
DIGEST_TOPIC = os.getenv("DIGEST_TOPIC", "latest cybersecurity news")
DIGEST_MAX_ITEMS = int(os.getenv("DIGEST_MAX_ITEMS", "12"))
GENERAL_SECURITY_MAX_ITEMS = int(os.getenv("GENERAL_SECURITY_MAX_ITEMS", "15"))
DIGEST_TIME = os.getenv("DIGEST_TIME", "08:00")
OLLAMA_TOOL_ITERATIONS = int(os.getenv("OLLAMA_TOOL_ITERATIONS", "6"))
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
LOG_FILE = os.getenv("LOG_FILE", "logs/news_digest.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
APP_VENDOR_REFRESH_CACHE = False
APP_VENDOR_CONTEXT_CACHE = None


def setup_logger():
    log_path = Path(LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("openfang_news")
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    if logger.handlers:
        return logger

    handler = RotatingFileHandler(
        log_path,
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(handler)
    return logger


LOGGER = setup_logger()


def ollama_client():
    return ollama.Client(host=OLLAMA_HOST)


def env_list(name, default=""):
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def ollama_chat(messages):
    client = ollama_client()
    response = client.chat(
        model=OLLAMA_MODEL,
        messages=messages,
        stream=False,
        options={"temperature": 0.2},
    )
    return response["message"]["content"].strip()


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

    LOGGER.info("web_search_start query=%r max_results=%s", query, max_results)
    payload = json.dumps(
        {"query": query, "max_results": min(int(max_results), 10)}
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://ollama.com/api/web_search",
        data=payload,
        headers={
            "Authorization": f"Bearer {OLLAMA_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=45) as http_response:
        data = json.loads(http_response.read().decode("utf-8"))

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
        "and include a source link.\n\n"
        f"{vendor_section}"
        "## Defensive Actions\n"
        "Write concise recommended actions for defenders.\n\n"
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

    for _ in range(OLLAMA_TOOL_ITERATIONS):
        response = client.chat(
            model=OLLAMA_MODEL,
            messages=messages,
            tools=tools,
            stream=False,
            options={"temperature": 0.2, "num_ctx": 32768},
        )
        message = response["message"]
        messages.append(message)

        tool_calls = message_value(message, "tool_calls", []) or []
        if not tool_calls:
            content = message_value(message, "content", "")
            if content:
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


def generate_search_queries():
    today, yesterday = digest_date_window()
    app_vendors, app_vendor_context = get_app_vendor_context()
    LOGGER.info(
        "query_planner_start model=%r topic=%r max_items=%s",
        OLLAMA_MODEL,
        DIGEST_TOPIC,
        DIGEST_MAX_ITEMS,
    )
    content = ollama_chat(
        [
            {
                "role": "system",
                "content": (
                    "You choose web search queries for a cybersecurity news digest. "
                    "Return only search queries, one per line. Do not add bullets, "
                    "numbers, explanations, or quotes."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Today is {today}. Create diverse web search queries for "
                    f"finding {DIGEST_TOPIC} from today and yesterday ({yesterday}). "
                    f"Include queries for the top {GENERAL_SECURITY_MAX_ITEMS} general "
                    "company security stories, ransomware, exploited vulnerabilities, "
                    "data breaches, and government/vendor advisories."
                    + (
                        "\n\nAlso include targeted queries for recent cybersecurity "
                        "issues involving these non-duplicated app vendors: "
                        f"{app_vendor_context}."
                        if app_vendors
                        else ""
                    )
                ),
            },
        ]
    )
    queries = []
    for line in content.splitlines():
        query = line.strip().lstrip("-*0123456789. ").strip()
        if query and query not in queries:
            queries.append(query)
    if not queries:
        raise RuntimeError("Ollama did not produce any web search queries.")
    query_limit = 8 if app_vendors else 5
    for query in queries[:query_limit]:
        LOGGER.info("query_planner_query query=%r", query)
    return queries[:query_limit]


def collect_ollama_web_search_results(queries):
    seen = set()
    results = []
    per_query = max(1, min(10, WEB_SEARCH_RESULT_LIMIT // max(1, len(queries)) + 2))
    for query in queries:
        response = web_search(query=query, max_results=per_query)
        for result in response.results:
            url = result.url
            if url in seen:
                continue
            seen.add(url)
            results.append(
                {
                    "query": query,
                    "title": result.title,
                    "url": url,
                    "content": result.content,
                }
            )
            if len(results) >= WEB_SEARCH_RESULT_LIMIT:
                return results
    return results


def summarize_search_results(results):
    if not results:
        return "No recent cybersecurity articles were found today."

    lines = []
    for index, result in enumerate(results, start=1):
        lines.append(
            "\n".join(
                [
                    f"{index}. {result['title']}",
                    f"Search query: {result['query']}",
                    f"URL: {result['url']}",
                    f"Snippet: {result['content']}",
                ]
            )
        )

    today, yesterday = digest_date_window()
    app_vendors, _ = get_app_vendor_context()
    return ollama_chat(
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
                    f"{chr(10).join(lines)}\n\n"
                    f"{digest_format_instructions(bool(app_vendors))}"
                ),
            },
        ]
    )


def run_ollama_query_planner_search():
    queries = generate_search_queries()
    results = collect_ollama_web_search_results(queries)
    LOGGER.info("query_planner_results_count count=%s", len(results))
    return summarize_search_results(results)


def fallback_digest(ollama_error):
    LOGGER.exception("digest_failed error=%s", ollama_error)
    today = datetime.now().strftime("%B %d, %Y")
    lines = [
        f"Daily Cybersecurity News Digest - {today}",
        "",
        f"Ollama web-search digest was unavailable: {ollama_error}",
        "",
        "No fallback feeds are configured. This script now relies on LLM-directed Ollama web_search.",
        "Check that Ollama is running locally, the model is available, and OLLAMA_API_KEY is set for web search.",
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

    for raw_line in markdown.splitlines():
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
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        if not stripped:
            close_paragraph()
            close_lists()
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
            continue

        close_lists()
        paragraph_lines.append(line)

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
        LOGGER.warning("tool_agent_failed error=%s", tool_exc)
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
            return fallback_digest(f"tool calling failed: {tool_exc}; LLM-directed web search failed: {search_exc}")


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
