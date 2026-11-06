#!/usr/bin/env python3
"""web_server.py - a FastMCP server giving the model web search and fetch.

Two tools the agent offers to the model, so it can reach the live web when the
answer is not in its own weights or in the RAG index:

  web_search(query, k)  - query a self-hosted SearXNG instance and return the
                          top results (title, url, snippet).
  web_fetch(url)        - fetch one page and return its readable text.

The search and fetch calls go straight from this server to SearXNG and the web,
but the *results* travel back through FortiAIGate as the tool's output and are
inspected there - so the gateway sees whatever the web returns before it reaches
the model's context.

Run it directly to smoke-test, or let the agent launch it over stdio via
FAIG_MCP_CONFIG (see mcp.config.web.example.json). Stand SearXNG up first
(agent/searxng/) and point WEB_SEARXNG_URL at it.

Config (environment variables, all optional):
    WEB_SEARXNG_URL         SearXNG base URL.            Default http://localhost:8080
    WEB_TOP_K               Default results to return.   Default 5
    WEB_TIMEOUT             HTTP timeout in seconds.     Default 20
    WEB_FETCH_MAX_CHARS     Cap on page text returned.   Default 6000
    WEB_FETCH_ALLOW_PRIVATE Allow private/loopback URLs. Default 0 (blocked)
    WEB_ENGINES             Restrict SearXNG engines, e.g. "duckduckgo,brave".
"""
from __future__ import annotations

import ipaddress
import os
import socket
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit("The 'mcp' package is missing. Install it with: pip install mcp") from exc

SEARXNG_URL = os.environ.get("WEB_SEARXNG_URL", "http://localhost:8080").rstrip("/")
TOP_K = int(os.environ.get("WEB_TOP_K", "5"))
TIMEOUT = float(os.environ.get("WEB_TIMEOUT", "20"))
FETCH_MAX_CHARS = int(os.environ.get("WEB_FETCH_MAX_CHARS", "6000"))
ALLOW_PRIVATE = os.environ.get("WEB_FETCH_ALLOW_PRIVATE", "0") == "1"
ENGINES = os.environ.get("WEB_ENGINES", "").strip()

# A plain, non-identifying User-Agent for outbound fetches.
USER_AGENT = "Mozilla/5.0 (compatible; FortiAIGate-lab-agent)"

mcp = FastMCP("web-tools")


# --------------------------------------------------------------------------
# HTML -> text (small, self-contained; no extra dependencies)
# --------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg", "head"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg", "head") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return "\n".join(parser.parts)


# --------------------------------------------------------------------------
# SSRF guard: refuse to fetch internal addresses unless explicitly allowed.
# Keeps the model from being steered into the Proxmox UI, the FortiAIGate
# admin, or anything else on the lab network.
# --------------------------------------------------------------------------

def _is_blocked_host(host: str) -> str | None:
    """Return a reason string if host resolves to a non-public address, else None."""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception as exc:
        return f"could not resolve host ({exc})"
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return f"{host} resolves to a non-public address ({addr})"
    return None


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

@mcp.tool()
def web_search(query: str, k: int = TOP_K) -> dict:
    """Search the live web for current or external information via the lab's SearXNG.

    Use this when the answer is not in your own knowledge or in the indexed
    documentation - for recent events, current versions, prices, or anything
    time-sensitive. Returns the top results, each with a title, URL and snippet.
    Follow up with web_fetch(url) to read a promising result in full.
    """
    k = max(1, min(int(k), 10))
    params = {"q": query, "format": "json"}
    if ENGINES:
        params["engines"] = ENGINES
    try:
        resp = httpx.get(f"{SEARXNG_URL}/search", params=params, timeout=TIMEOUT,
                         headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        hint = " (is JSON output enabled in SearXNG settings.yml?)" if exc.response.status_code == 403 else ""
        return {"error": f"search failed: HTTP {exc.response.status_code}{hint}"}
    except Exception as exc:
        return {"error": f"search failed ({type(exc).__name__}: {exc}). Is SearXNG running at {SEARXNG_URL}?"}

    results = []
    for item in (data.get("results") or [])[:k]:
        results.append({
            "title": item.get("title"),
            "url": item.get("url"),
            "snippet": (item.get("content") or "").strip(),
        })
    if not results:
        return {"query": query, "results": [], "note": "no web results"}
    return {"query": query, "results": results}


@mcp.tool()
def web_fetch(url: str, max_chars: int = FETCH_MAX_CHARS) -> dict:
    """Fetch one web page and return its readable text.

    Use this after web_search to read a result in full. Give an http(s) URL.
    Returns the page's extracted text, truncated to keep the context manageable.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return {"error": f"unsupported URL scheme '{parts.scheme}'; use http or https"}
    if not parts.hostname:
        return {"error": "no host in URL"}
    if not ALLOW_PRIVATE:
        blocked = _is_blocked_host(parts.hostname)
        if blocked:
            return {"error": f"refusing to fetch: {blocked}. "
                             "Set WEB_FETCH_ALLOW_PRIVATE=1 to allow internal addresses."}
    try:
        resp = httpx.get(url, timeout=TIMEOUT, follow_redirects=True,
                         headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
    except Exception as exc:
        return {"error": f"fetch failed ({type(exc).__name__}: {exc})"}

    ctype = resp.headers.get("content-type", "")
    if "html" in ctype or not ctype:
        text = _html_to_text(resp.text)
    elif ctype.startswith("text/") or "json" in ctype or "xml" in ctype:
        text = resp.text
    else:
        return {"url": str(resp.url), "content_type": ctype,
                "note": "not a text/HTML document; nothing extracted"}

    text = text.strip()
    truncated = len(text) > max_chars
    return {
        "url": str(resp.url),
        "chars": len(text),
        "truncated": truncated,
        "text": text[:max_chars],
    }


if __name__ == "__main__":
    mcp.run()
