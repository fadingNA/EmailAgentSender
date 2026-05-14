#!/usr/bin/env python3
"""
news_digest.py  –  Cybersecurity digest with per-item LLM analysis.

Analysis pipeline
-----------------
General news : one ollama_chat call per article  → GeneralItem (JSON)
Vendor news  : articles grouped by vendor name   → one ollama_chat call per vendor → VendorItem (JSON)
Assembly     : all JSON objects combined → render_digest_markdown() → email HTML
"""

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
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from email.message import EmailMessage
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

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


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

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
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "nemotron3:33b")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "")
_ollama_timeout_raw = os.getenv("OLLAMA_CHAT_TIMEOUT_SECONDS", "none")
OLLAMA_CHAT_TIMEOUT_SECONDS = (
    None if _ollama_timeout_raw.lower() == "none" else float(_ollama_timeout_raw)
)

DEFAULT_APP_VENDOR_DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_APP_VENDOR_CACHE_PATH = DEFAULT_APP_VENDOR_DATA_DIR / ".cache" / "app_vendors.json"
DIGEST_TOPIC = os.getenv("DIGEST_TOPIC", "latest cybersecurity news")
DIGEST_MAX_ITEMS = int(os.getenv("DIGEST_MAX_ITEMS", "12"))
GENERAL_SECURITY_MAX_ITEMS = int(os.getenv("GENERAL_SECURITY_MAX_ITEMS", "15"))
DIGEST_TIME = os.getenv("DIGEST_TIME", "08:00")
OLLAMA_TOOL_ITERATIONS = int(os.getenv("OLLAMA_TOOL_ITERATIONS", "3"))
ENABLE_TOOL_AGENT_FALLBACK = os.getenv("ENABLE_TOOL_AGENT_FALLBACK", "0").lower() in {
    "1",
    "true",
    "yes",
}
APP_VENDOR_DATA_DIR = os.getenv("APP_VENDOR_DATA_DIR", str(DEFAULT_APP_VENDOR_DATA_DIR))
APP_VENDOR_COLUMN = os.getenv("APP_VENDOR_COLUMN", "app_vendor")
APP_VENDOR_LIMIT = int(os.getenv("APP_VENDOR_LIMIT", "15"))
APP_VENDOR_CACHE_PATH = os.getenv("APP_VENDOR_CACHE_PATH", str(DEFAULT_APP_VENDOR_CACHE_PATH))
WEB_SEARCH_RESULT_LIMIT = int(os.getenv("WEB_SEARCH_RESULT_LIMIT", 15))
WEB_SEARCH_RETRIES = int(os.getenv("WEB_SEARCH_RETRIES", "1"))
WEB_SEARCH_RETRY_DELAY_SECONDS = float(os.getenv("WEB_SEARCH_RETRY_DELAY_SECONDS", "45"))
WEB_SEARCH_REQUEST_DELAY_SECONDS = float(os.getenv("WEB_SEARCH_REQUEST_DELAY_SECONDS", "25"))
WEB_SEARCH_ENABLED = os.getenv("WEB_SEARCH_ENABLED", "1").lower() in {"1", "true", "yes"}
WEB_SEARCH_STOP_TRACK_ON_EMPTY = os.getenv("WEB_SEARCH_STOP_TRACK_ON_EMPTY", "1").lower() in {
    "1",
    "true",
    "yes",
}
WEB_SEARCH_STOP_TRACK_ON_RATE_LIMIT = os.getenv(
    "WEB_SEARCH_STOP_TRACK_ON_RATE_LIMIT", "1"
).lower() in {"1", "true", "yes"}
WEB_SEARCH_RATE_LIMIT_COOLDOWN_SECONDS = float(
    os.getenv("WEB_SEARCH_RATE_LIMIT_COOLDOWN_SECONDS", "60")
)
VENDOR_SEARCH_QUERIES_PER_VENDOR = int(os.getenv("VENDOR_SEARCH_QUERIES_PER_VENDOR", "3"))
STALE_CUTOFF_DAYS = int(os.getenv("STALE_CUTOFF_DAYS", "2"))
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


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Analysis result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class CVEDetail:
    """Structured CVE / vulnerability record."""
    cve_id: str = "Not stated"
    cvss_score: str = "Not stated"
    severity: str = "Not stated"
    vuln_class: str = "Not stated"
    attack_vector: str = "Not stated"
    exploitation_status: str = "Not stated"
    affected_product: str = "Not stated"
    affected_versions: str = "Not stated"
    fixed_version: str = "Not stated"
    impact: str = "Not stated"


@dataclass
class GeneralItem:
    """One analysed general-security news article."""
    rank: int = 0
    title: str = ""
    source_url: str = ""          # Always set from raw search result — never from LLM
    raw_title: str = ""           # Original headline from search result, preserved as fallback
    organization: str = "Not stated"
    why_it_matters: str = ""
    severity: str = "Not stated"  # Info / Low / Medium / High / Critical
    cves: list[CVEDetail] = field(default_factory=list)
    patch_priority: str = "Not stated"
    defender_action: str = ""
    skipped: bool = False          # True when the article had no security value
    skip_reason: str = ""


@dataclass
class VendorFinding:
    """One CVE / incident finding for a vendor."""
    affected_product: str = "Not stated"
    affected_versions: str = "Not stated"
    cve_id: str = "Not stated"
    cvss_score: str = "Not stated"
    severity: str = "Not stated"
    vuln_class: str = "Not stated"
    impact: str = "Not stated"
    exploitation_status: str = "Not stated"
    patched_version: str = "Not stated"
    workaround: str = "Not stated"
    defender_action: str = "Not stated"
    source_url: str = ""


@dataclass
class VendorItem:
    """Aggregated analysis for one vendor."""
    vendor_name: str = ""
    summary: str = ""
    findings: list[VendorFinding] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)   # Pinned from raw results
    raw_articles: list[dict] = field(default_factory=list) # Full raw results — guaranteed fallback
    skipped: bool = False
    skip_reason: str = ""


# ---------------------------------------------------------------------------
# JSON schema strings (injected into prompts so the model knows the shape)
# ---------------------------------------------------------------------------

_GENERAL_ITEM_SCHEMA = """\
{
  "rank": <int, 1-based position>,
  "title": "<article headline>",
  "source_url": "<url>",
  "organization": "<company / product / org name>",
  "why_it_matters": "<1-2 sentence plain-English explanation>",
  "severity": "<Info|Low|Medium|High|Critical>",
  "cves": [
    {
      "cve_id": "<CVE-YYYY-NNNNN or Not stated>",
      "cvss_score": "<numeric or Not stated>",
      "severity": "<Info|Low|Medium|High|Critical or Not stated>",
      "vuln_class": "<e.g. RCE / SQLi / XSS or Not stated>",
      "attack_vector": "<Network|Adjacent|Local|Physical or Not stated>",
      "exploitation_status": "<Exploited in wild|PoC available|No known exploit|Not stated>",
      "affected_product": "<product name or Not stated>",
      "affected_versions": "<version range or Not stated>",
      "fixed_version": "<fixed version or Not stated>",
      "impact": "<brief impact description or Not stated>"
    }
  ],
  "patch_priority": "<Immediate|High|Moderate|Low|Not applicable>",
  "defender_action": "<concrete one-line action for defenders>",
  "skipped": false,
  "skip_reason": ""
}"""

_VENDOR_ITEM_SCHEMA = """\
{
  "vendor_name": "<vendor name>",
  "summary": "<2-3 sentence overview of all current findings for this vendor>",
  "findings": [
    {
      "affected_product": "<product name or Not stated>",
      "affected_versions": "<version range or Not stated>",
      "cve_id": "<CVE-YYYY-NNNNN or Not stated>",
      "cvss_score": "<numeric or Not stated>",
      "severity": "<Info|Low|Medium|High|Critical or Not stated>",
      "vuln_class": "<e.g. RCE / Auth bypass or Not stated>",
      "impact": "<brief impact or Not stated>",
      "exploitation_status": "<Exploited in wild|PoC available|No known exploit|Not stated>",
      "patched_version": "<fixed version or Not stated>",
      "workaround": "<workaround or Not stated>",
      "defender_action": "<concrete action for this finding>",
      "source_url": "<url>"
    }
  ],
  "source_urls": ["<url1>", "<url2>"],
  "skipped": false,
  "skip_reason": ""
}"""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class EmptyWebSearchResponse(RuntimeError):
    pass


class RateLimitedWebSearchResponse(RuntimeError):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# Ollama helpers
# ---------------------------------------------------------------------------

def ollama_client():
    return ollama.Client(host=OLLAMA_HOST, timeout=OLLAMA_CHAT_TIMEOUT_SECONDS)


def env_list(name, default=""):
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def ollama_chat(messages) -> str:
    """Single non-streaming chat call. Returns the text content."""
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
        options={"temperature": 0.2, "num_ctx": 32768},
    )
    content = response["message"]["content"].strip()
    LOGGER.info(
        "ollama_chat_complete model=%r duration_seconds=%.1f chars=%s",
        OLLAMA_MODEL,
        time.monotonic() - start,
        len(content),
    )
    return content


def _parse_json_response(raw: str, label: str) -> dict:
    """
    Parse a JSON object from a model response that may contain preamble prose
    or markdown fences before/after the JSON.

    Strategy:
      1. Fast path — strip outer fences and try direct parse.
      2. Slow path — find the first balanced { ... } block in the raw string,
         handling models that emit "Sure, here is the result:\\n```json\\n{...}".
    Returns an empty dict on total failure (caller decides how to handle).
    """
    # Fast path: strip leading/trailing fences
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Slow path: find first '{' and walk to its matching '}'
    start = raw.find("{")
    if start != -1:
        depth = 0
        for i, ch in enumerate(raw[start:], start=start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = raw[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError as exc:
                        LOGGER.warning(
                            "json_parse_slow_path_failed label=%r error=%s preview=%r",
                            label,
                            exc,
                            candidate[:300],
                        )
                    break

    LOGGER.warning("json_parse_failed label=%r no_valid_json preview=%r", label, raw[:300])
    return {}


# ---------------------------------------------------------------------------
# Per-item analysis: GeneralItem
# ---------------------------------------------------------------------------

_GENERAL_SYSTEM_PROMPT = (
    "You are a cybersecurity analyst producing structured JSON. "
    "Analyse the supplied article snippet and return ONLY a single valid JSON object "
    "matching the schema exactly. Do not add prose, markdown fences, or extra keys. "
    "Use 'Not stated' for any field the article does not provide. "
    "If the article has no security relevance set skipped=true and skip_reason to a short reason."
)


def analyse_general_article(rank: int, result: dict) -> GeneralItem:
    """
    Send one article to the LLM for analysis. Returns a GeneralItem dataclass.

    Source URL and raw title are pinned from the search result BEFORE the LLM
    call and are never overwritten by the model response — this guarantees
    references are always present in the final email regardless of what the
    LLM returns.
    """
    # Pin these from the raw result immediately — LLM output never overrides them
    pinned_url = result.get("url", "").strip()
    pinned_title = result.get("title", "").strip()

    label = f"general_rank={rank} url={pinned_url}"
    LOGGER.info("analyse_general_start %s", label)

    snippet = "\n".join([
        f"Title: {pinned_title}",
        f"URL: {pinned_url}",
        f"Snippet: {result.get('content', '')[:1200]}",
    ])

    raw = ollama_chat([
        {"role": "system", "content": _GENERAL_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Analyse this cybersecurity article and return a JSON object "
                f"matching this schema:\n{_GENERAL_ITEM_SCHEMA}\n\n"
                f"Article:\n{snippet}"
            ),
        },
    ])

    data = _parse_json_response(raw, label)
    if not data:
        LOGGER.warning("analyse_general_fallback %s", label)
        return GeneralItem(
            rank=rank,
            title=pinned_title,
            source_url=pinned_url,
            raw_title=pinned_title,
            why_it_matters="Analysis unavailable.",
            skipped=True,
            skip_reason="LLM returned unparseable JSON",
        )

    cves = [
        CVEDetail(**{k: v for k, v in cve.items() if k in CVEDetail.__dataclass_fields__})
        for cve in data.get("cves", [])
        if isinstance(cve, dict)
    ]
    item = GeneralItem(
        rank=rank,
        # Always use the raw search result URL — LLM may hallucinate or truncate URLs
        source_url=pinned_url,
        # Prefer LLM-cleaned title (it may fix encoding) but fall back to raw
        title=data.get("title") or pinned_title,
        raw_title=pinned_title,
        organization=data.get("organization", "Not stated"),
        why_it_matters=data.get("why_it_matters", ""),
        severity=data.get("severity", "Not stated"),
        cves=cves,
        patch_priority=data.get("patch_priority", "Not stated"),
        defender_action=data.get("defender_action", ""),
        skipped=bool(data.get("skipped", False)),
        skip_reason=data.get("skip_reason", ""),
    )
    LOGGER.info(
        "analyse_general_complete %s skipped=%s cves=%s",
        label,
        item.skipped,
        len(item.cves),
    )
    return item


def analyse_general_articles(results: list[dict]) -> list[GeneralItem]:
    """
    Analyse each general-security article individually.
    Articles are processed sequentially; skipped items are filtered out unless
    every item was skipped (in which case we keep them all so the digest is not empty).
    """
    items: list[GeneralItem] = []
    for index, result in enumerate(results, start=1):
        item = analyse_general_article(rank=index, result=result)
        items.append(item)

    active = [i for i in items if not i.skipped]
    LOGGER.info(
        "analyse_general_batch_complete total=%s active=%s skipped=%s",
        len(items),
        len(active),
        len(items) - len(active),
    )
    return active if active else items


# ---------------------------------------------------------------------------
# Per-vendor analysis: VendorItem
# ---------------------------------------------------------------------------

_VENDOR_SYSTEM_PROMPT = (
    "You are a cybersecurity analyst producing structured JSON. "
    "You will receive a list of article snippets all related to one vendor. "
    "Analyse them collectively and return ONLY a single valid JSON object "
    "matching the schema exactly. Do not add prose, markdown fences, or extra keys. "
    "Use 'Not stated' for any field the articles do not provide. "
    "If none of the articles contain a real security finding for this vendor, "
    "set skipped=true and skip_reason to a short reason."
)


def analyse_vendor_articles(vendor_name: str, results: list[dict]) -> VendorItem:
    """
    Send all articles for one vendor in a single LLM call. Returns a VendorItem.

    Raw article URLs are pinned in source_urls and raw_articles before the LLM
    call. After the call, any VendorFinding with a missing source_url is
    backfilled with the closest matching raw article URL so references are
    never silently dropped.
    """
    label = f"vendor={vendor_name!r} articles={len(results)}"
    LOGGER.info("analyse_vendor_start %s", label)

    # Pin URLs from raw results NOW — independent of anything the LLM returns
    pinned_urls = [r.get("url", "").strip() for r in results if r.get("url", "").strip()]

    # Build numbered snippets so the LLM can reference articles by index
    snippets = []
    for idx, r in enumerate(results, start=1):
        snippets.append(
            f"[{idx}] Title: {r.get('title', '')}\n"
            f"    URL: {r.get('url', '')}\n"
            f"    Snippet: {r.get('content', '')[:800]}"
        )

    raw = ollama_chat([
        {"role": "system", "content": _VENDOR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Vendor: {vendor_name}\n\n"
                f"Analyse these {len(results)} article(s) and return a JSON object "
                f"matching this schema:\n{_VENDOR_ITEM_SCHEMA}\n\n"
                f"Articles:\n" + "\n\n".join(snippets)
            ),
        },
    ])

    data = _parse_json_response(raw, label)
    if not data:
        LOGGER.warning("analyse_vendor_fallback %s", label)
        return VendorItem(
            vendor_name=vendor_name,
            source_urls=pinned_urls,
            raw_articles=results,
            skipped=True,
            skip_reason="LLM returned unparseable JSON",
        )

    findings_raw = [f for f in data.get("findings", []) if isinstance(f, dict)]

    # Backfill any finding missing a source_url:
    # try to match by title keyword against raw articles; fall back to
    # the first raw article URL so the reference is never blank.
    url_by_title: dict[str, str] = {
        r.get("title", "").casefold(): r.get("url", "")
        for r in results
        if r.get("url")
    }

    def _best_url_for_finding(finding: dict) -> str:
        llm_url = finding.get("source_url", "").strip()
        if llm_url:
            return llm_url
        # Try to match the finding's product/cve against article titles
        needle = " ".join([
            finding.get("affected_product", ""),
            finding.get("cve_id", ""),
        ]).casefold().strip()
        for raw_title_lower, raw_url in url_by_title.items():
            if needle and any(word in raw_title_lower for word in needle.split() if len(word) > 3):
                LOGGER.debug(
                    "vendor_finding_url_backfill vendor=%r product=%r matched_title=%r",
                    vendor_name,
                    finding.get("affected_product"),
                    raw_title_lower,
                )
                return raw_url
        # Last resort: use the first pinned URL
        fallback = pinned_urls[0] if pinned_urls else ""
        if fallback:
            LOGGER.debug(
                "vendor_finding_url_fallback vendor=%r product=%r fallback_url=%r",
                vendor_name,
                finding.get("affected_product"),
                fallback,
            )
        return fallback

    findings = []
    for f_dict in findings_raw:
        f_dict["source_url"] = _best_url_for_finding(f_dict)
        findings.append(
            VendorFinding(
                **{k: v for k, v in f_dict.items() if k in VendorFinding.__dataclass_fields__}
            )
        )

    item = VendorItem(
        vendor_name=data.get("vendor_name") or vendor_name,
        summary=data.get("summary", ""),
        findings=findings,
        source_urls=pinned_urls,          # Always from raw results, never from LLM
        raw_articles=results,
        skipped=bool(data.get("skipped", False)),
        skip_reason=data.get("skip_reason", ""),
    )
    LOGGER.info(
        "analyse_vendor_complete %s skipped=%s findings=%s source_urls=%s",
        label,
        item.skipped,
        len(item.findings),
        len(item.source_urls),
    )
    return item


def analyse_vendor_results(vendor_results: list[dict]) -> list[VendorItem]:
    """
    Group raw search results by vendor name, then call analyse_vendor_articles
    once per vendor.

    Articles that cannot be matched to any known vendor are dropped rather than
    accumulated into a misleading "Unknown Vendor" bucket.
    """
    # Single call — previous code called this twice and discarded one result each time
    app_vendors_list, _ = get_app_vendor_context()

    # Build a lowercase → canonical name map from the known vendor list
    vendor_name_map: dict[str, str] = {
        str(v).strip().casefold(): str(v).strip() for v in app_vendors_list
    }

    grouped: dict[str, list[dict]] = {}
    unmatched = 0
    for result in vendor_results:
        # Try to find which vendor this result belongs to via title/content
        searchable = f"{result.get('title', '')} {result.get('content', '')}".casefold()
        matched_vendor: str | None = None
        for canon_lower, canon_name in vendor_name_map.items():
            if canon_lower and canon_lower in searchable:
                matched_vendor = canon_name
                break

        if matched_vendor is None:
            # Drop: not attributable to any known vendor; don't pollute analysis
            unmatched += 1
            LOGGER.info(
                "vendor_grouping_no_match url=%r title=%r",
                result.get("url", ""),
                result.get("title", "")[:80],
            )
            continue

        grouped.setdefault(matched_vendor, []).append(result)

    LOGGER.info(
        "vendor_grouping_complete vendor_count=%s matched_articles=%s unmatched_dropped=%s",
        len(grouped),
        len(vendor_results) - unmatched,
        unmatched,
    )

    items: list[VendorItem] = []
    for vendor_name, articles in grouped.items():
        item = analyse_vendor_articles(vendor_name, articles)
        items.append(item)

    active = [i for i in items if not i.skipped]
    LOGGER.info(
        "analyse_vendor_batch_complete total=%s active=%s skipped=%s",
        len(items),
        len(active),
        len(items) - len(active),
    )
    return active if active else items


# ---------------------------------------------------------------------------
# Markdown rendering from structured objects
# ---------------------------------------------------------------------------

def _severity_badge(severity: str) -> str:
    """Return a short inline severity label (used in markdown)."""
    sev = severity.strip().upper()
    labels = {
        "CRITICAL": "🔴 Critical",
        "HIGH": "🟠 High",
        "MEDIUM": "🟡 Medium",
        "LOW": "🟢 Low",
        "INFO": "ℹ️ Info",
    }
    return labels.get(sev, severity)


def render_general_items_markdown(items: list[GeneralItem]) -> str:
    """Render a numbered markdown list for the general security section."""
    if not items:
        return "_No general security stories were found today._\n"

    lines: list[str] = []
    for item in items:
        lines.append(f"{item.rank}. **{item.organization}** – {item.title}")
        lines.append(f"   {_severity_badge(item.severity)} | Patch priority: {item.patch_priority}")
        lines.append(f"   {item.why_it_matters}")

        if item.cves:
            if len(item.cves) == 1:
                cve = item.cves[0]
                lines.append(
                    f"   CVE: `{cve.cve_id}` | CVSS: {cve.cvss_score} | "
                    f"Class: {cve.vuln_class} | Vector: {cve.attack_vector} | "
                    f"Exploitation: {cve.exploitation_status} | "
                    f"Fixed in: {cve.fixed_version}"
                )
            else:
                lines.append("")
                lines.append(
                    "   | Product | CVE ID | CVSS | Severity | Class | "
                    "Exploitation | Fixed In |"
                )
                lines.append(
                    "   |---------|--------|------|----------|-------|"
                    "--------------|---------|"
                )
                for cve in item.cves:
                    lines.append(
                        f"   | {cve.affected_product} | `{cve.cve_id}` | "
                        f"{cve.cvss_score} | {cve.severity} | {cve.vuln_class} | "
                        f"{cve.exploitation_status} | {cve.fixed_version} |"
                    )

        if item.defender_action:
            lines.append(f"   🛡️ **Action:** {item.defender_action}")

        lines.append(f"   🔗 [{item.title}]({item.source_url})")
        lines.append("")

    return "\n".join(lines)


def render_vendor_items_markdown(items: list[VendorItem]) -> str:
    """Render the vendor watch section as grouped markdown."""
    if not items:
        return "_No targeted vendor findings were identified today._\n"

    lines: list[str] = []
    for item in items:
        lines.append(f"### {item.vendor_name}")
        if item.summary:
            lines.append(item.summary)
            lines.append("")

        if item.findings:
            lines.append(
                "| Product | CVE ID | CVSS | Severity | Class | "
                "Exploitation | Patched | Defender Action |"
            )
            lines.append(
                "|---------|--------|------|----------|-------|"
                "--------------|---------|----------------|"
            )
            for f in item.findings:
                lines.append(
                    f"| {f.affected_product} ({f.affected_versions}) "
                    f"| `{f.cve_id}` | {f.cvss_score} | {f.severity} "
                    f"| {f.vuln_class} | {f.exploitation_status} "
                    f"| {f.patched_version} | {f.defender_action} |"
                )
            lines.append("")

        if item.source_urls:
            refs = " · ".join(f"[source]({url})" for url in item.source_urls[:5])
            lines.append(f"Sources: {refs}")
            lines.append("")

    return "\n".join(lines)


def render_defensive_actions_markdown(
    general_items: list[GeneralItem],
    vendor_items: list[VendorItem],
) -> str:
    """Combine defender actions from all analysed items into one section."""
    lines: list[str] = []

    critical_high = [i for i in general_items if i.severity.upper() in ("CRITICAL", "HIGH")]
    medium_low = [i for i in general_items if i.severity.upper() in ("MEDIUM", "LOW")]

    if critical_high:
        lines.append("**Immediate / High Priority**")
        for item in critical_high:
            if item.defender_action:
                cve_refs = ", ".join(f"`{c.cve_id}`" for c in item.cves if c.cve_id != "Not stated")
                ref = f" ({cve_refs})" if cve_refs else ""
                lines.append(f"- {item.defender_action}{ref}")
        lines.append("")

    if medium_low:
        lines.append("**Moderate Priority**")
        for item in medium_low:
            if item.defender_action:
                lines.append(f"- {item.defender_action}")
        lines.append("")

    if vendor_items:
        lines.append("**Vendor-Specific Actions**")
        for vi in vendor_items:
            for f in vi.findings:
                if f.defender_action and f.defender_action != "Not stated":
                    cve_ref = f" (`{f.cve_id}`)" if f.cve_id != "Not stated" else ""
                    lines.append(f"- **{vi.vendor_name}** – {f.defender_action}{cve_ref}")
        lines.append("")

    if not lines:
        lines.append("_No specific defender actions identified._")

    return "\n".join(lines)


def render_references_markdown(
    general_items: list[GeneralItem],
    vendor_items: list[VendorItem],
) -> str:
    """
    Build the References section from structured objects.

    References are sourced in priority order:
      1. GeneralItem.source_url  — pinned from raw search result, always present
      2. VendorFinding.source_url — backfilled in analyse_vendor_articles
      3. VendorItem.source_urls  — pinned from raw search results
      4. VendorItem.raw_articles — last-resort fallback; guarantees no vendor
         is silently dropped from references even if all URL fields ended up blank
    """
    seen: set[str] = set()
    lines: list[str] = []
    index = 1

    # --- General items ---
    for item in general_items:
        url = item.source_url.strip()
        if not url:
            # Should never happen after the pinning fix, but guard anyway
            LOGGER.warning(
                "render_references_missing_url rank=%s title=%r", item.rank, item.raw_title
            )
            continue
        if url not in seen:
            seen.add(url)
            # Use raw_title as label so the link text is always the original headline
            display = item.raw_title or item.title or url
            lines.append(f"{index}. [{display}]({url})")
            index += 1

    # --- Vendor items ---
    for vi in vendor_items:
        # 1. Per-finding URLs (backfilled, so these should almost always be set)
        for f in vi.findings:
            url = f.source_url.strip()
            if url and url not in seen:
                seen.add(url)
                label = f"{vi.vendor_name}"
                if f.affected_product and f.affected_product != "Not stated":
                    label += f" – {f.affected_product}"
                if f.cve_id and f.cve_id != "Not stated":
                    label += f" ({f.cve_id})"
                lines.append(f"{index}. [{label}]({url})")
                index += 1

        # 2. Pinned source_urls from raw results (covers the vendor-level summary)
        for url in vi.source_urls:
            url = url.strip()
            if url and url not in seen:
                seen.add(url)
                lines.append(f"{index}. [{vi.vendor_name} – source]({url})")
                index += 1

        # 3. Raw article fallback — guarantees at least one reference per vendor
        #    even if findings had no URLs and source_urls was somehow empty
        for article in vi.raw_articles:
            url = article.get("url", "").strip()
            title = article.get("title", "").strip()
            if url and url not in seen:
                seen.add(url)
                label = title or f"{vi.vendor_name} – source"
                lines.append(f"{index}. [{label}]({url})")
                index += 1

    if not lines:
        return "_No references available._"

    LOGGER.info("render_references_complete count=%s", len(lines))
    return "\n".join(lines)


def render_executive_snapshot(
    general_items: list[GeneralItem],
    vendor_items: list[VendorItem],
) -> str:
    """
    Ask the LLM to write the 3-5 bullet executive snapshot from the
    already-structured objects (much cheaper than re-analysing everything).
    """
    summary_data = {
        "general": [
            {
                "title": i.title,
                "organization": i.organization,
                "severity": i.severity,
                "why_it_matters": i.why_it_matters,
                "cve_count": len(i.cves),
            }
            for i in general_items[:GENERAL_SECURITY_MAX_ITEMS]
        ],
        "vendors": [
            {
                "vendor": v.vendor_name,
                "summary": v.summary,
                "finding_count": len(v.findings),
            }
            for v in vendor_items
        ],
    }

    raw = ollama_chat([
        {
            "role": "system",
            "content": (
                "You are a cybersecurity analyst. Write exactly 3-5 concise bullet points "
                "summarising the most important takeaways from the structured digest data below. "
                "Return only the bullet points, each starting with '- '. No headers, no preamble."
            ),
        },
        {
            "role": "user",
            "content": f"Digest summary data:\n{json.dumps(summary_data, indent=2)}",
        },
    ])
    return raw.strip()


def render_digest_markdown(
    general_items: list[GeneralItem],
    vendor_items: list[VendorItem],
) -> str:
    """
    Combine all analysed objects into the final digest markdown that
    markdown_to_email_html() already knows how to render.
    """
    today, _ = digest_date_window()
    has_vendors = bool(vendor_items)

    executive_snapshot = render_executive_snapshot(general_items, vendor_items)
    general_md = render_general_items_markdown(general_items)
    vendor_md = render_vendor_items_markdown(vendor_items) if has_vendors else (
        "## 2. Targeted Vendor Watch – All Relevant Matches\n"
        "No targeted vendor list was available.\n"
    )
    defensive_md = render_defensive_actions_markdown(general_items, vendor_items)
    references_md = render_references_markdown(general_items, vendor_items)

    return "\n".join([
        "# Cybersecurity Briefing",
        "",
        "## Executive Snapshot",
        executive_snapshot,
        "",
        f"## 1. General Company Security – Top {GENERAL_SECURITY_MAX_ITEMS} (Today & Yesterday)",
        general_md,
        "",
        f"## 2. Targeted Vendor Watch – All Relevant Matches",
        vendor_md,
        "",
        "## Defensive Actions",
        defensive_md,
        "",
        "## References",
        references_md,
    ])


# ---------------------------------------------------------------------------
# Message helpers
# ---------------------------------------------------------------------------

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


def retry_after_seconds(headers):
    raw_value = headers.get("Retry-After") if headers else None
    if not raw_value:
        return None
    try:
        return max(0.0, float(raw_value))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Web search
# ---------------------------------------------------------------------------

def web_search(query: str, max_results: int = 5):
    """Search the web for current source material."""
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
            retry_after = retry_after_seconds(exc.headers)
            LOGGER.warning(
                "web_search_http_error query=%r status=%s retry_after=%s body_preview=%r",
                query,
                exc.code,
                retry_after,
                response_body[:500],
            )
            if exc.code == 429:
                cooldown = retry_after or WEB_SEARCH_RATE_LIMIT_COOLDOWN_SECONDS
                if WEB_SEARCH_STOP_TRACK_ON_RATE_LIMIT:
                    raise RateLimitedWebSearchResponse(
                        f"Ollama web_search rate limited query: {query}",
                        retry_after=cooldown,
                    ) from exc
                if attempt < max_attempts:
                    LOGGER.warning(
                        "web_search_rate_limited_retry query=%r cooldown_seconds=%.1f",
                        query,
                        cooldown,
                    )
                    time.sleep(cooldown)
                    continue
                raise RateLimitedWebSearchResponse(
                    f"Ollama web_search rate limited query: {query}",
                    retry_after=cooldown,
                ) from exc
            if exc.code in {500, 502, 503, 504} and attempt < max_attempts:
                time.sleep(WEB_SEARCH_RETRY_DELAY_SECONDS * attempt)
                continue
            raise

        if not response_body.strip():
            LOGGER.warning("web_search_empty_response query=%r attempt=%s", query, attempt)
            if attempt < max_attempts:
                time.sleep(WEB_SEARCH_RETRY_DELAY_SECONDS * attempt)
                continue
            raise EmptyWebSearchResponse(
                f"Ollama web_search returned an empty response for query: {query}"
            )

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


# ---------------------------------------------------------------------------
# App vendor context
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def digest_date_window():
    today = datetime.now()
    yesterday = today - timedelta(days=1)
    return (
        today.strftime("%B %d, %Y"),
        yesterday.strftime("%B %d, %Y"),
    )


# Recognised date formats found in feed/search result content.
_DATE_PATTERNS = [
    (re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"), "%Y-%m-%d"),
    (re.compile(r"\b(\d{2}/\d{2}/\d{4})\b"), "%m/%d/%Y"),
    (re.compile(r"\b(\w{3,9}\s+\d{1,2},?\s+\d{4})\b"), "%B %d %Y"),   # "May 13 2026"
    (re.compile(r"\b(\d{1,2}\s+\w{3,9}\s+\d{4})\b"), "%d %B %Y"),      # "13 May 2026"
]


def _is_fresh(result: dict) -> bool:
    """
    Return True if the result appears to be from within STALE_CUTOFF_DAYS days.

    Checks the 'published' field first (set by RSS feed parser), then scans
    the title + content for any recognisable date string.  If no date can be
    parsed at all we assume the article is fresh so we never drop content just
    because the search snippet didn't include a dateline.
    """
    cutoff = datetime.now() - timedelta(days=STALE_CUTOFF_DAYS)

    def _try_parse(text: str) -> datetime | None:
        # RFC 2822 / HTTP date (common in RSS pubDate)
        # e.g. "Tue, 13 May 2026 08:00:00 +0000"
        rfc_match = re.search(
            r"\b\d{1,2}\s+\w{3}\s+\d{4}\s+\d{2}:\d{2}:\d{2}", text
        )
        if rfc_match:
            try:
                return datetime.strptime(rfc_match.group(0), "%d %b %Y %H:%M:%S")
            except ValueError:
                pass
        for pattern, fmt in _DATE_PATTERNS:
            for match in pattern.findall(text):
                # Normalise: strip commas, collapse multiple spaces
                normalised = re.sub(r",", "", match).strip()
                normalised = re.sub(r"\s+", " ", normalised)
                try:
                    return datetime.strptime(normalised, fmt)
                except ValueError:
                    continue
        return None

    # Prefer the explicit published field the feed parser set
    published = result.get("published", "")
    if published:
        dt = _try_parse(published)
        if dt is not None:
            fresh = dt >= cutoff
            if not fresh:
                LOGGER.info(
                    "freshness_stale published=%r cutoff=%s url=%r",
                    published,
                    cutoff.date(),
                    result.get("url", ""),
                )
            return fresh

    # Fall back to scanning title + content snippet
    searchable = f"{result.get('title', '')} {result.get('content', '')[:400]}"
    dt = _try_parse(searchable)
    if dt is not None:
        fresh = dt >= cutoff
        if not fresh:
            LOGGER.info(
                "freshness_stale_content dt=%s cutoff=%s url=%r",
                dt.date(),
                cutoff.date(),
                result.get("url", ""),
            )
        return fresh

    # No date found — assume fresh, never drop on uncertainty
    LOGGER.debug("freshness_unknown_assume_fresh url=%r", result.get("url", ""))
    return True


# ---------------------------------------------------------------------------
# Search pipeline
# ---------------------------------------------------------------------------

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
    for index, query in enumerate(queries, start=1):
        if index > 1 and WEB_SEARCH_REQUEST_DELAY_SECONDS > 0:
            LOGGER.info(
                "web_search_request_pause group=%s delay_seconds=%.1f",
                source_group,
                WEB_SEARCH_REQUEST_DELAY_SECONDS,
            )
            time.sleep(WEB_SEARCH_REQUEST_DELAY_SECONDS)
        try:
            response = web_search(query=query, max_results=per_query)
        except RateLimitedWebSearchResponse as exc:
            LOGGER.warning(
                "query_web_search_rate_limited group=%s query=%r retry_after=%s error=%s",
                source_group,
                query,
                exc.retry_after,
                exc,
            )
            if WEB_SEARCH_STOP_TRACK_ON_RATE_LIMIT:
                LOGGER.warning(
                    "search_track_aborted group=%s reason=web_search_rate_limited retry_after=%s",
                    source_group,
                    exc.retry_after,
                )
                break
            continue
        except EmptyWebSearchResponse as exc:
            LOGGER.warning(
                "query_web_search_empty group=%s query=%r error=%s",
                source_group,
                query,
                exc,
            )
            if WEB_SEARCH_STOP_TRACK_ON_EMPTY:
                LOGGER.warning(
                    "search_track_aborted group=%s reason=empty_web_search_response",
                    source_group,
                )
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
            entry = {
                "group": source_group,
                "query": query,
                "title": result.title,
                "url": url,
                "content": result.content,
            }
            if not _is_fresh(entry):
                LOGGER.info(
                    "web_search_stale_dropped group=%s url=%r title=%r",
                    source_group,
                    url,
                    result.title[:80],
                )
                continue
            results.append(entry)
            if len(results) >= WEB_SEARCH_RESULT_LIMIT:
                LOGGER.info(
                    "search_track_complete group=%s results=%s", source_group, len(results)
                )
                return results

    if not results:
        LOGGER.warning("search_track_empty group=%s", source_group)
        return []
    LOGGER.info("search_track_complete group=%s results=%s", source_group, len(results))
    return results


# ---------------------------------------------------------------------------
# RSS feed fallback
# ---------------------------------------------------------------------------

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
    LOGGER.info(
        "feed_fetch_response url=%r status=%s bytes=%s", feed_url, response.status, len(body)
    )
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
        except (
            ET.ParseError,
            OSError,
            UnicodeDecodeError,
            urllib.error.URLError,
            TimeoutError,
        ) as exc:
            LOGGER.warning("feed_fetch_failed url=%r error=%s", feed_url, exc)
            continue

        for entry in entries:
            url = entry["url"]
            if url in seen:
                continue
            seen.add(url)

            # RSS entries carry a published field — best place to apply freshness check
            feed_result = {
                "query": entry["query"],
                "title": entry["title"],
                "url": url,
                "published": entry.get("published", ""),
                "content": " ".join(
                    part for part in [entry.get("published"), entry["content"]] if part
                ),
            }
            if not _is_fresh(feed_result):
                LOGGER.info(
                    "feed_stale_dropped url=%r published=%r title=%r",
                    url,
                    entry.get("published", ""),
                    entry["title"][:80],
                )
                continue

            searchable_text = f"{entry['title']} {entry['content']}".casefold()
            if len(general_results) < GENERAL_SECURITY_MAX_ITEMS:
                general_results.append({"group": "general", **feed_result})
            if vendor_terms and any(vendor in searchable_text for vendor in vendor_terms):
                vendor_results.append({"group": "vendor", **feed_result})

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


# ---------------------------------------------------------------------------
# Main digest generation — new per-item pipeline
# ---------------------------------------------------------------------------

def run_ollama_query_planner_search() -> str:
    """
    New pipeline:
      1. Web search for general + vendor articles (unchanged)
      2. Analyse each general article individually → GeneralItem[]
      3. Analyse each vendor group collectively  → VendorItem[]
      4. Combine into final markdown
    """
    app_vendors, _ = get_app_vendor_context()
    LOGGER.info(
        "two_track_search_start topic=%r general_max_items=%s vendor_count=%s",
        DIGEST_TOPIC,
        GENERAL_SECURITY_MAX_ITEMS,
        len(app_vendors),
    )

    # --- Search phase ---
    general_queries = generate_general_search_queries()
    general_raw = collect_ollama_web_search_results(general_queries, "general")

    vendor_raw: list[dict] = []
    if app_vendors:
        vendor_queries = generate_vendor_search_queries(app_vendors)
        vendor_raw = collect_ollama_web_search_results(vendor_queries, "vendor")
        # Drop any vendor result whose URL already appeared in general results.
        # The same article (e.g. a critical Microsoft CVE) would otherwise be
        # analysed twice and appear in both sections of the email.
        general_urls = {r["url"] for r in general_raw}
        before = len(vendor_raw)
        vendor_raw = [r for r in vendor_raw if r["url"] not in general_urls]
        dropped = before - len(vendor_raw)
        if dropped:
            LOGGER.info(
                "cross_dedup_vendor_raw dropped=%s remaining=%s", dropped, len(vendor_raw)
            )
    else:
        LOGGER.info("vendor_search_skipped reason=no_app_vendors")

    all_raw = general_raw + vendor_raw
    if not all_raw:
        LOGGER.warning("two_track_web_search_empty action=curated_feed_fallback")
        all_raw = collect_curated_feed_results(app_vendors)

    if not all_raw:
        raise RuntimeError(
            "No web search results were collected from Ollama web search or curated feeds."
        )

    # --- Analysis phase ---
    LOGGER.info(
        "analysis_phase_start general_articles=%s vendor_articles=%s",
        len(general_raw),
        len(vendor_raw),
    )

    # Cap general articles to avoid excessive LLM calls
    capped_general = general_raw[:GENERAL_SECURITY_MAX_ITEMS]
    general_items = analyse_general_articles(capped_general)
    vendor_items = analyse_vendor_results(vendor_raw) if vendor_raw else []

    LOGGER.info(
        "analysis_phase_complete general_items=%s vendor_items=%s",
        len(general_items),
        len(vendor_items),
    )

    # --- Render phase ---
    digest = render_digest_markdown(general_items, vendor_items)
    LOGGER.info("digest_render_complete chars=%s", len(digest))
    return digest


# ---------------------------------------------------------------------------
# Tool-agent fallback (unchanged from original)
# ---------------------------------------------------------------------------

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
    available_tools = {"web_search": web_search}
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


# ---------------------------------------------------------------------------
# Fallback digest (no LLM results at all)
# ---------------------------------------------------------------------------

def fallback_digest(ollama_error):
    LOGGER.error("digest_failed error=%s", ollama_error)
    today = datetime.now().strftime("%B %d, %Y")
    lines = [
        f"Daily Cybersecurity News Digest - {today}",
        "",
        f"Cybersecurity digest source collection was unavailable: {ollama_error}",
        "",
        "Check the terminal logs for Ollama web_search empty responses, "
        "curated feed fetch failures, or local model timeout errors.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Email rendering (unchanged)
# ---------------------------------------------------------------------------

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
                    "background:#f7fafc;border:1px solid #dbe5ef;border-left:4px solid #155eef;"
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
                blocks.append(f'<{tag} style="{list_style}">')
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
                      <div style="font-size:12px;letter-spacing:0.08em;text-transform:uppercase;color:#8bd3ff;font-weight:800">Cybersecurity News</div>
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


# ---------------------------------------------------------------------------
# Email sending (unchanged)
# ---------------------------------------------------------------------------

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
        raise RuntimeError(
            "Set MAILERSEND_TO or SMTP_TO to one or more comma-separated recipient emails."
        )
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
                    raise RuntimeError(
                        f"MailerSend returned HTTP {response.status}: {response_body}"
                    )
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


# ---------------------------------------------------------------------------
# Top-level orchestration (unchanged interface)
# ---------------------------------------------------------------------------

def generate_digest() -> str:
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
            return fallback_digest(
                f"two-track web search failed: {search_exc}; "
                f"tool calling failed: {tool_exc}"
            )


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
            LOGGER.error("daemon_digest_failed error=%s", exc, exc_info=True)
            # Attempt to send a plain failure notification so the inbox signals
            # the problem even when the terminal is unattended.
            try:
                send_email(
                    fallback_digest(str(exc)),
                    subject=f"⚠️ Digest Failed – {datetime.now().strftime('%Y-%m-%d')}",
                )
                LOGGER.info("daemon_failure_notification_sent")
            except Exception as notify_exc:
                # Can't send — at least the log above captured the root cause.
                LOGGER.error("daemon_failure_notification_failed error=%s", notify_exc)
        time.sleep(60)


def print_cron(time_hhmm):
    hour, minute = [int(part) for part in time_hhmm.split(":", 1)]
    script_path = Path(__file__).resolve()
    python_path = "/usr/bin/env python3"
    print(f"{minute} {hour} * * * cd {script_path.parent} && {python_path} {script_path}")


# ---------------------------------------------------------------------------
# CLI (unchanged)
# ---------------------------------------------------------------------------

def main():
    global APP_VENDOR_REFRESH_CACHE

    parser = argparse.ArgumentParser(
        description=(
            "Send a daily Ollama-generated cybersecurity news digest "
            "to multiple email recipients."
        )
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the digest instead of emailing it."
    )
    parser.add_argument(
        "--test-send-mail",
        action="store_true",
        help="Send a simple test email without fetching news.",
    )
    parser.add_argument(
        "--daemon", action="store_true", help="Keep running and send once per day."
    )
    parser.add_argument(
        "--time", default=DIGEST_TIME, help="Daily send time in 24-hour HH:MM format."
    )
    parser.add_argument(
        "--print-cron", action="store_true", help="Print a crontab line for daily automation."
    )
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
