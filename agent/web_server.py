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
(agent/searxng/) and point WEB_SEARXNG_URL at it. Set it in the server's "env"
block in mcp.config.json: the agent starts MCP servers over stdio with a minimal
environment, so a variable exported in your shell does not reach this process.

Config (environment variables, all optional):
    WEB_SEARXNG_URL         SearXNG base URL.            Default http://localhost:8888
    WEB_TOP_K               Default results to return.   Default 5
    WEB_TIMEOUT             HTTP timeout in seconds.     Default 20
    WEB_FETCH_MAX_CHARS     Cap on page text returned.   Default 3000
    WEB_FETCH_ALLOW_PRIVATE Allow private/loopback URLs. Default 0 (blocked)
    WEB_ENGINES             Restrict SearXNG engines, e.g. "duckduckgo,brave".
"""
from __future__ import annotations

import ipaddress
import os
import socket
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

import httpx

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit("The 'mcp' package is missing. Install it with: pip install mcp") from exc

SEARXNG_URL = os.environ.get("WEB_SEARXNG_URL", "http://localhost:8888").rstrip("/")
TOP_K = int(os.environ.get("WEB_TOP_K", "5"))
TIMEOUT = float(os.environ.get("WEB_TIMEOUT", "20"))
FETCH_MAX_CHARS = int(os.environ.get("WEB_FETCH_MAX_CHARS", "3000"))
# The model may ask for more, but a few large pages overflow an 8K context
# and push the question itself out - so its request is capped here.
FETCH_HARD_CAP = int(os.environ.get("WEB_FETCH_HARD_CAP", "4000"))
ALLOW_PRIVATE = os.environ.get("WEB_FETCH_ALLOW_PRIVATE", "0") == "1"
ENGINES = os.environ.get("WEB_ENGINES", "").strip()

# A plain, non-identifying User-Agent for outbound fetches.
USER_AGENT = "Mozilla/5.0 (compatible; FortiAIGate-lab-agent)"

mcp = FastMCP("web-tools")


# --------------------------------------------------------------------------
# HTML -> text (small, self-contained; no extra dependencies)
# --------------------------------------------------------------------------

# Elements that hold page chrome rather than content.
_SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "nav", "header", "footer",
              "aside", "form", "button", "select", "template", "iframe"}
_SKIP_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search", "menu"}
_SKIP_CLASS_WORDS = ("navbox", "sidebar", "mw-editsection", "noprint", "vector-header",
                     "vector-menu", "vector-toc", "vector-page-toolbar", "catlinks",
                     "mw-footer", "cookie", "breadcrumb", "skip-link", "reference")
# Elements that usually wrap the article itself.
_MAIN_IDS = {"content", "main-content", "maincontent", "mw-content-text", "bodycontent", "article"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
         "param", "source", "track", "wbr"}
_BLOCK = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section",
          "article", "table", "dd", "dt", "pre", "blockquote"}


class _TextExtractor(HTMLParser):
    """Collects readable text, skipping menus and other page chrome.

    Keeps two streams: all body text, and text inside the page's main content
    element (<main>, <article>, or a known content id such as Wikipedia's).
    Also remembers where an element with a given id starts, so a URL fragment
    (#Section) can jump to that section.
    """

    def __init__(self, fragment: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.fragment = fragment
        self.stack: list[tuple[str, bool, bool]] = []    # (tag, skipping, in_main)
        self.parts: list[str] = []
        self.main_parts: list[str] = []
        self.frag_all: int | None = None
        self.frag_main: int | None = None

    def _skip(self, tag, attrs) -> bool:
        if tag in _SKIP_TAGS:
            return True
        a = dict(attrs)
        if (a.get("role") or "").lower() in _SKIP_ROLES:
            return True
        if a.get("aria-hidden") == "true" or "hidden" in a:
            return True
        cls = (a.get("class") or "").lower()
        return any(word in cls for word in _SKIP_CLASS_WORDS)

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            return
        skipping, in_main = (self.stack[-1][1], self.stack[-1][2]) if self.stack else (False, False)
        skipping = skipping or self._skip(tag, attrs)
        a = dict(attrs)
        if not in_main and (tag in ("main", "article") or (a.get("id") or "").lower() in _MAIN_IDS):
            in_main = True
        if self.fragment and a.get("id") == self.fragment and self.frag_all is None:
            self.frag_all, self.frag_main = len(self.parts), len(self.main_parts)
        self.stack.append((tag, skipping, in_main))
        if tag in _BLOCK and not skipping:
            self._newline()

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):        # tolerate unclosed tags
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        if tag in _BLOCK:
            self._newline()

    def _newline(self):
        for buf in (self.parts, self.main_parts):
            if buf and buf[-1] != "\n":
                buf.append("\n")

    def handle_data(self, data):
        if not data.strip():
            return
        skipping, in_main = (self.stack[-1][1], self.stack[-1][2]) if self.stack else (False, False)
        if skipping:
            return
        text = " ".join(data.split())
        self.parts.append(text)
        if in_main:
            self.main_parts.append(text)

    def text(self) -> str:
        use_main = len("".join(self.main_parts)) >= 500
        buf = self.main_parts if use_main else self.parts
        start = (self.frag_main if use_main else self.frag_all) if self.frag_all is not None else 0
        out = " ".join(buf[start or 0:])
        out = "\n".join(line.strip() for line in out.split("\n"))
        return "\n".join(line for line in out.splitlines() if line)


def _html_to_text(html: str, fragment: str = "") -> str:
    parser = _TextExtractor(fragment)
    parser.feed(html)
    parser.close()
    return parser.text()


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
    time-sensitive. Returns up to 6 results, each with a title, URL, snippet and,
    when known, a publication date. Follow up with web_fetch(url) to read a
    promising result in full.

    Write short keyword queries (3 to 6 words) naming specific things: products,
    companies, CVE numbers, years, and words like "breach", "incident",
    "vulnerability". Avoid quotes and full sentences. Search one topic per call.
    If results are generic, rephrase with different specific terms instead of
    repeating the same query.
    """
    k = max(1, min(int(k), 6))
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

    results, seen = [], set()
    for item in data.get("results") or []:
        url = item.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        entry = {"title": item.get("title"), "url": url,
                 "snippet": (item.get("content") or "").strip()[:300]}
        if item.get("publishedDate"):
            entry["published"] = str(item["publishedDate"])[:10]
        results.append(entry)
        if len(results) >= k:
            break

    # Direct answers and infoboxes (e.g. a Wikipedia summary) can arrive even
    # when the ordinary result engines are rate-limited or timed out - keep them.
    extras = []
    for box in data.get("infoboxes") or []:
        content = (box.get("content") or "").strip()
        if content:
            extras.append({"title": box.get("infobox"), "url": box.get("id"),
                           "snippet": content})
    for ans in data.get("answers") or []:
        text = ans.get("answer") if isinstance(ans, dict) else ans
        if text:
            extras.append({"title": "answer",
                           "url": ans.get("url") if isinstance(ans, dict) else None,
                           "snippet": str(text).strip()})

    out = {"query": query, "results": results}
    if extras:
        out["infoboxes_and_answers"] = extras[:3]
    if not results and not extras:
        down = [e[0] for e in data.get("unresponsive_engines") or []]
        out["note"] = "no web results" + (f"; unresponsive engines: {', '.join(down)}" if down else "")
    return out


@mcp.tool()
def web_fetch(url: str, max_chars: int = FETCH_MAX_CHARS) -> dict:
    """Fetch one web page and return its readable text.

    Use this after web_search to read a result in full. Give an http(s) URL.
    Menus and other page chrome are dropped, and a #fragment (for example a
    Wikipedia section such as .../Hugging_Face#History) starts the text at that
    section. Returns the extracted text, truncated to keep the context manageable.
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
        text = _html_to_text(resp.text, unquote(parts.fragment))
    elif ctype.startswith("text/") or "json" in ctype or "xml" in ctype:
        text = resp.text
    else:
        return {"url": str(resp.url), "content_type": ctype,
                "note": "not a text/HTML document; nothing extracted"}

    text = text.strip()
    max_chars = max(500, min(int(max_chars), FETCH_HARD_CAP))
    truncated = len(text) > max_chars
    out = {
        "url": str(resp.url),
        "chars": len(text),
        "truncated": truncated,
        "text": text[:max_chars],
    }
    if len(text) < 400:
        out["note"] = ("very little text extracted - this page is probably built by "
                       "JavaScript (common for live results dashboards). Answer from the "
                       "search snippets instead of fetching similar pages.")
    return out


if __name__ == "__main__":
    mcp.run()
