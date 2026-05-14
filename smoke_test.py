#!/usr/bin/env python3
"""Smoke test: fire one web search query and print the results."""
import sys
from send_mail import web_search, OLLAMA_API_KEY, WEB_SEARCH_RESULT_LIMIT, RateLimitedWebSearchResponse

def main():
    if not OLLAMA_API_KEY:
        print("ERROR: OLLAMA_API_KEY is not set in .env", file=sys.stderr)
        sys.exit(1)

    query = "cybersecurity vulnerability CVE 2026"
    print(f"Query : {query}")
    print(f"Limit : {WEB_SEARCH_RESULT_LIMIT}")
    print("-" * 60)

    try:
        response = web_search(query=query, max_results=3)
    except RateLimitedWebSearchResponse as exc:
        wait = int(exc.retry_after or 0)
        mins = wait // 60
        print(f"RATE LIMITED — retry_after={wait}s (~{mins} min). API key is valid but hourly quota is exhausted.")
        sys.exit(2)

    if not response.results:
        print("No results returned.")
        sys.exit(1)

    for i, r in enumerate(response.results, 1):
        print(f"\n[{i}] {r.title}")
        print(f"    URL     : {r.url}")
        print(f"    Snippet : {r.content[:120]}...")

    print(f"\nOK — {len(response.results)} result(s) returned.")

if __name__ == "__main__":
    main()
