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
    WEB_SEARCH_RETRIES      Retries when every engine timed out.  Default 2
    WEB_SEARCH_RETRY_DELAY  Seconds between those retries.        Default 3
    WEB_SEARCH_BACKEND      auto | tavily | searxng.  Default auto: Tavily when
                            TAVILY_API_KEY is set, SearXNG otherwise. Either way
                            SearXNG is the fallback if Tavily fails or finds nothing.
    TAVILY_API_KEY          Tavily key (tvly-...). Put it in this server's "env"
                            block in mcp.config.json, which git ignores.

With Tavily, search queries go to Tavily's API (a third party); the results
still reach the model only through FortiAIGate.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import time
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
SEARCH_RETRIES = int(os.environ.get("WEB_SEARCH_RETRIES", "2"))
SEARCH_RETRY_DELAY = float(os.environ.get("WEB_SEARCH_RETRY_DELAY", "3"))
BACKEND = os.environ.get("WEB_SEARCH_BACKEND", "auto").strip().lower()
TAVILY_KEY = os.environ.get("TAVILY_API_KEY", "").strip()
TAVILY_URL = os.environ.get("TAVILY_URL", "https://api.tavily.com/search")

# Words that mark a query about something that happened; such queries go to news
# search unless the model says otherwise. General search ranks definitions and
# home pages first, which is useless for incidents and events.
_EVENT_WORDS = ("incident", "breach", "attack", "hack", "leak", "exploit", "outage",
                "lawsuit", "ransomware", "vulnerab", "cve-", "escape", "compromis",
                "stolen", "fraud", "announce", "launch", "released", "news", "latest",
                "disclos", "fined", "bill", "post-mortem", "postmortem")

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

def _searx_query(params: dict) -> dict:
    resp = httpx.get(f"{SEARXNG_URL}/search", params=params, timeout=TIMEOUT,
                     headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.json()


def _searx_with_retry(params: dict) -> dict:
    """Query SearXNG; on a cold start (every engine timed out) pause and retry."""
    data = _searx_query(params)
    for _ in range(SEARCH_RETRIES):
        if data.get("results") or not _all_timed_out(data):
            break
        time.sleep(SEARCH_RETRY_DELAY)
        data = _searx_query(params)
    return data


def _all_timed_out(data: dict) -> bool:
    """True when no engine answered and every failure was a timeout.

    Covers "timeout" and "Suspended: timeout" (the brief suspension SearXNG
    applies after a cold-start timeout). Rate limits and CAPTCHAs, including
    "Suspended: too many requests", do not clear in seconds and are not retried.
    """
    down = data.get("unresponsive_engines") or []
    return bool(down) and all(
        "timeout" in str(reason).lower()
        for _, reason in (e if len(e) == 2 else (e, "") for e in down))


def _looks_like_event(query: str) -> bool:
    q = query.lower()
    return any(w in q for w in _EVENT_WORDS) or bool(re.search(r"\b20\d\d\b", q))


def _tavily_query(query: str, k: int, news: bool, time_range: str) -> dict:
    """Search with Tavily and return the result in SearXNG's shape."""
    body = {"query": query, "topic": "news" if news else "general", "max_results": k,
            "search_depth": "basic", "include_published_date": True}
    if time_range:
        body["time_range"] = time_range
    resp = httpx.post(TAVILY_URL, json=body, timeout=TIMEOUT,
                      headers={"Authorization": f"Bearer {TAVILY_KEY}"})
    resp.raise_for_status()
    data = resp.json()
    return {"results": [{"title": r.get("title"), "url": r.get("url"),
                         "content": r.get("content"), "publishedDate": r.get("published_date")}
                        for r in data.get("results") or []]}


def _searx_search(query: str, news: bool, time_range: str) -> dict:
    params = {"q": query, "format": "json"}
    if news:
        params["categories"] = "news"
    if time_range:
        params["time_range"] = time_range
    if ENGINES and not news:
        params["engines"] = ENGINES
    data = _searx_with_retry(params)
    if news and not data.get("results"):
        params.pop("categories", None)               # news empty: general web instead
        data = _searx_with_retry(params)
        data["note_fallback"] = True
    return data


@mcp.tool()
def web_search(query: str, k: int = TOP_K, news: bool | None = None, time_range: str = "") -> dict:
    """Search the live web for current or external information.

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

    news: true searches news for things that happened (incidents, breaches,
    attacks, outages, releases). Leave it unset and queries that mention such
    events, or a year, use news automatically; set false to force general search.
    time_range: "day", "week", "month" or "year" limits how old results may be
    (for "the past 12 months", use "year").
    """
    k = max(1, min(int(k), 6))
    use_news = _looks_like_event(query) if news is None else bool(news)
    tr = (time_range or "").strip().lower()
    tr = tr if tr in ("day", "week", "month", "year") else ""

    data, backend, notes = None, "searxng", []
    if BACKEND == "tavily" or (BACKEND == "auto" and TAVILY_KEY):
        if not TAVILY_KEY:
            notes.append("WEB_SEARCH_BACKEND=tavily but TAVILY_API_KEY is not set; used SearXNG")
        else:
            try:
                data = _tavily_query(query, k, use_news, tr)
                if use_news and not data["results"]:
                    data = _tavily_query(query, k, False, tr)
                    data["note_fallback"] = True
                backend = "tavily"
                if not data["results"]:
                    notes.append("Tavily found nothing; tried SearXNG")
                    data = None
            except httpx.HTTPStatusError as exc:
                notes.append(f"Tavily unavailable (HTTP {exc.response.status_code}: key, rate or monthly limit); used SearXNG")
            except Exception as exc:
                notes.append(f"Tavily unavailable ({type(exc).__name__}); used SearXNG")
    if data is None:
        backend = "searxng"
        try:
            data = _searx_search(query, use_news, tr)
        except httpx.HTTPStatusError as exc:
            hint = " (is JSON output enabled in SearXNG settings.yml?)" if exc.response.status_code == 403 else ""
            return {"error": f"search failed: HTTP {exc.response.status_code}{hint}", "notes": notes}
        except Exception as exc:
            return {"error": f"search failed ({type(exc).__name__}: {exc}). Is SearXNG running at {SEARXNG_URL}?",
                    "notes": notes}

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

    out = {"query": query, "backend": backend,
           "mode": "web" if data.get("note_fallback") or not use_news else "news",
           "results": results}
    if data.get("note_fallback"):
        out["fallback"] = "news search found nothing, so these are general web results"
    if notes:
        out["notes"] = notes
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
