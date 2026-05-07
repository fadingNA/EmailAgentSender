#!/usr/bin/env python3
import argparse
import html
import json
import logging
import os
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

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:9b")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "")

DIGEST_TOPIC = os.getenv("DIGEST_TOPIC", "latest cybersecurity news")
DIGEST_MAX_ITEMS = int(os.getenv("DIGEST_MAX_ITEMS", "12"))
DIGEST_TIME = os.getenv("DIGEST_TIME", "08:00")
OLLAMA_TOOL_ITERATIONS = int(os.getenv("OLLAMA_TOOL_ITERATIONS", "6"))
LOG_FILE = os.getenv("LOG_FILE", "logs/news_digest.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


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


def run_ollama_web_search_agent():
    LOGGER.info(
        "digest_tool_agent_start model=%r topic=%r max_items=%s",
        OLLAMA_MODEL,
        DIGEST_TOPIC,
        DIGEST_MAX_ITEMS,
    )
    client = ollama_client()
    today = datetime.now().strftime("%B %d, %Y")
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
                "source used."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Today is {today}. Create a daily briefing about {DIGEST_TOPIC}. "
                "Freely search the web for the latest important cybersecurity "
                f"news. Try multiple useful queries if needed and use up to "
                f"{DIGEST_MAX_ITEMS} strong results. Format the final answer with: "
                "1. Executive summary, 3-5 bullets. 2. Top stories, each with why "
                "it matters and source links. 3. Recommended defensive actions. "
                "4. References."
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
    today = datetime.now().strftime("%B %d, %Y")
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
                    f"finding {DIGEST_TOPIC}. Include breaking cybersecurity news, "
                    "ransomware, vulnerability exploitation, data breaches, and "
                    "government/vendor advisories where relevant."
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
    for query in queries[:4]:
        LOGGER.info("query_planner_query query=%r", query)
    return queries[:4]


def collect_ollama_web_search_results(queries):
    seen = set()
    results = []
    per_query = max(1, min(10, DIGEST_MAX_ITEMS // max(1, len(queries)) + 1))
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
            if len(results) >= DIGEST_MAX_ITEMS:
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

    today = datetime.now().strftime("%B %d, %Y")
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
                    f"Today is {today}. Create a daily briefing about {DIGEST_TOPIC}.\n\n"
                    f"{chr(10).join(lines)}\n\n"
                    "Format the final answer with: 1. Executive summary, 3-5 bullets. "
                    "2. Top stories, each with why it matters and a source link. "
                    "3. Recommended defensive actions. 4. References."
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


def make_email(subject, body, recipients, sender):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    html_body = "<br>".join(html.escape(line) for line in body.splitlines())
    message.add_alternative(
        f"<html><body><pre style=\"font-family:Arial,sans-serif;white-space:pre-wrap\">{html_body}</pre></body></html>",
        subtype="html",
    )
    return message


def send_email(body):
    recipients = env_list("SMTP_TO", SMTP_TO)
    if not recipients:
        raise RuntimeError("Set SMTP_TO to one or more comma-separated recipient emails.")
    if not SMTP_USER or not SMTP_PASSWORD:
        raise RuntimeError("Set SMTP_USER and SMTP_PASSWORD for the SMTP account.")
    if not SMTP_FROM:
        raise RuntimeError("Set SMTP_FROM or SMTP_USER for the sender address.")

    subject = f"Daily Cybersecurity News - {datetime.now().strftime('%Y-%m-%d')}"
    message = make_email(subject, body, recipients, SMTP_FROM)

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls(context=context)
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(message)

    return recipients


def generate_digest():
    try:
        return run_ollama_web_search_agent()
    except (
        RuntimeError,
        ollama.RequestError,
        ollama.ResponseError,
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
    LOGGER.info("test_send_mail_start provider=smtp")
    body = "\n".join(
        [
            "OpenFang News test email",
            "",
            f"Sent at: {datetime.now().isoformat(timespec='seconds')}",
            "Provider: smtp",
            "",
            "If you received this, the email sending path is configured correctly.",
        ]
    )
    recipients = send_email(body)
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
    parser = argparse.ArgumentParser(
        description="Send a daily Ollama-generated cybersecurity news digest to multiple email recipients."
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the digest instead of emailing it.")
    parser.add_argument("--test-send-mail", action="store_true", help="Send a simple test email without fetching news.")
    parser.add_argument("--daemon", action="store_true", help="Keep running and send once per day.")
    parser.add_argument("--time", default=DIGEST_TIME, help="Daily send time in 24-hour HH:MM format.")
    parser.add_argument("--print-cron", action="store_true", help="Print a crontab line for daily automation.")
    args = parser.parse_args()

    if args.print_cron:
        print_cron(args.time)
    elif args.test_send_mail:
        run_test_send_mail()
    elif args.daemon:
        run_daemon(args.time)
    else:
        run_once(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
