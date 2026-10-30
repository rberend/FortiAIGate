#!/usr/bin/env python3
"""faig_web.py - a web interface for the FortiAIGate test agent.

It serves one chat page and relays each prompt through FortiAIGate using the
agent core in faig_agent.py, which must sit in the same directory. The virtual
key stays in this process; the browser never receives it.

Setup (once, same environment as faig_agent.py):
    python3 -m venv ~/faig-agent && ~/faig-agent/bin/pip install openai

Run:
    FAIG_CA_CERT=~/faig-tls.crt ~/faig-agent/bin/python faig_web.py --host 0.0.0.0
    then open http://<this-server>:8080

Configuration (environment variables, same as faig_agent.py):
    FAIG_BASE_URL, FAIG_MODEL, FAIG_CA_CERT, FAIG_API_KEY, FAIG_MCP_CONFIG

The page has no login. Anyone who can reach the port can send prompts with
your virtual key, so the default bind address is 127.0.0.1. Use --host 0.0.0.0
only on a lab network, and limit the port with a firewall rule.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

try:
    from faig_agent import (
        DEFAULT_BASE_URL,
        DEFAULT_MODEL,
        DEFAULT_SYSTEM,
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        ToolLoopError,
        build_client,
        error_parts,
        init_mcp_from_env,
        run_turn,
    )
except ImportError as exc:
    sys.exit(f"faig_web.py needs faig_agent.py in the same directory ({exc}).")

MAX_BODY_BYTES = 1_000_000
MAX_SYSTEM_CHARS = 8000


class App:
    def __init__(self, client, base_url: str, model: str):
        self.client = client
        self.base_url = base_url
        self.model = model
        self.gateway_host = urlsplit(base_url).hostname or base_url


def clean_history(raw) -> list[dict]:
    """Validate the conversation sent by the page and keep only known fields."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("The request has no messages.")
    history: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("A message is not an object.")
        role = item.get("role")
        content = item.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise ValueError("Message content must be text.")
        if role == "user":
            history.append({"role": "user", "content": content})
        elif role == "assistant":
            message = {"role": "assistant", "content": content}
            calls = item.get("tool_calls")
            if calls:
                if not isinstance(calls, list):
                    raise ValueError("tool_calls must be a list.")
                message["tool_calls"] = calls
            history.append(message)
        elif role == "tool":
            history.append({"role": "tool", "tool_call_id": str(item.get("tool_call_id", "")), "content": content})
        else:
            raise ValueError(f"Unsupported message role: {role!r}")
    if history[-1]["role"] != "user" or not history[-1]["content"].strip():
        raise ValueError("The last message must be a non-empty prompt.")
    return history


def chat_events(app: App, payload: dict):
    """Yield the events of one turn as dictionaries; always ends with done or error."""
    try:
        history = clean_history(payload.get("messages"))
    except ValueError as exc:
        yield {"type": "error", "kind": "agent", "message": str(exc)}
        return
    model = str(payload.get("model") or app.model)[:200]
    system = payload.get("system")
    system = system.strip()[:MAX_SYSTEM_CHARS] if isinstance(system, str) and system.strip() else DEFAULT_SYSTEM
    messages = [{"role": "system", "content": system}] + history
    yield {"type": "start", "model": model, "gateway": app.gateway_host}
    events = run_turn(app.client, model, messages,
                      stream=bool(payload.get("stream", True)), tools=bool(payload.get("tools", True)))
    try:
        while True:
            try:
                event = next(events)
            except StopIteration as stop:
                yield {"type": "done", "messages": stop.value}
                return
            yield event
    except ToolLoopError as exc:
        yield {"type": "error", "kind": "agent", "message": str(exc)}
    except APIStatusError as exc:
        status, code, message = error_parts(exc)
        yield {"type": "error", "kind": "gateway", "status": status, "code": code, "message": message}
    except APITimeoutError:
        yield {"type": "error", "kind": "timeout", "message": "The gateway did not answer in time."}
    except APIConnectionError as exc:
        cause = str(exc.__cause__ or exc)
        event = {"type": "error", "kind": "connection", "message": cause}
        if "CERTIFICATE_VERIFY_FAILED" in cause:
            event["hint"] = "Start faig_web.py with FAIG_CA_CERT set to the gateway's tls.crt."
        yield event
    except Exception as exc:  # keep the page informed instead of dropping the connection
        yield {"type": "error", "kind": "agent", "message": f"{type(exc).__name__}: {exc}"}
    finally:
        events.close()  # stops the upstream request if the browser went away


class Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "faig-web/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s %s\n" % (time.strftime("%H:%M:%S"), self.client_address[0], fmt % args))

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        return not origin or urlsplit(origin).netloc == self.headers.get("Host")

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8", {
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                           "script-src 'unsafe-inline'; connect-src 'self'; "
                                           "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
                "Referrer-Policy": "no-referrer",
            })
        elif path == "/api/config":
            self._json(200, {"model": self.app.model, "gateway": self.app.gateway_host,
                             "base_url": self.app.base_url, "system": DEFAULT_SYSTEM})
        elif path == "/api/models":
            try:
                names = [model.id for model in self.app.client.models.list().data]
                self._json(200, {"models": names or [self.app.model], "default": self.app.model})
            except APIStatusError as exc:
                status, code, message = error_parts(exc)
                self._json(200, {"models": [self.app.model], "default": self.app.model,
                                 "error": f"HTTP {status} {code} {message}".strip()})
            except APIConnectionError as exc:
                self._json(200, {"models": [self.app.model], "default": self.app.model,
                                 "error": str(exc.__cause__ or exc)})
        else:
            self._json(404, {"error": "Not found."})

    def do_POST(self):
        if urlsplit(self.path).path != "/api/chat":
            return self._json(404, {"error": "Not found."})
        if not self._same_origin():
            return self._json(403, {"error": "Cross-origin requests are not accepted."})
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self._json(415, {"error": "Send application/json."})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY_BYTES:
            return self._json(413 if length > MAX_BODY_BYTES else 400, {"error": "The request body is missing or too large."})
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            return self._json(400, {"error": "The request body is not a JSON object."})

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        outcome = "client went away"
        events = chat_events(self.app, payload)
        try:
            for event in events:
                self.wfile.write((json.dumps(event) + "\n").encode())
                self.wfile.flush()
                if event["type"] == "done":
                    outcome = "passed"
                elif event["type"] == "error":
                    status = event.get("status")
                    outcome = f"{event['kind']} error" + (f" HTTP {status}" if status else "")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            events.close()
        self.log_message("chat turn: %s", outcome)


def address_towards(host: str) -> str | None:
    """The local address this machine uses to reach `host`, for the startup hint."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((host, 443))
            return probe.getsockname()[0]
    except OSError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Web interface for the FortiAIGate test agent.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to listen on (default 127.0.0.1; use 0.0.0.0 to serve the lab network)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--base-url", default=os.environ.get("FAIG_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default=os.environ.get("FAIG_MODEL", DEFAULT_MODEL))
    parser.add_argument("--ca-cert", default=os.environ.get("FAIG_CA_CERT"), help="path to the gateway's tls.crt")
    parser.add_argument("--timeout", type=float, default=180.0, help="seconds per gateway request")
    args = parser.parse_args()

    api_key = os.environ.get("FAIG_API_KEY") or getpass.getpass("FortiAIGate virtual key: ").strip()
    if not api_key:
        print("No API key given.", file=sys.stderr)
        return 2

    client = build_client(args.base_url, api_key, args.ca_cert, args.timeout)
    app = App(client, args.base_url, args.model)

    try:
        bridge = init_mcp_from_env()
    except Exception as exc:  # a bad MCP config should not stop the console
        print(f"MCP disabled ({exc}).", file=sys.stderr)
        bridge = None
    if bridge is not None:
        print(f"MCP tools: {', '.join(sorted(bridge.names())) or 'none'}")
        for err in bridge.errors():
            print(f"  MCP server error: {err}", file=sys.stderr)

    print(f"Gateway: {args.base_url}")
    try:
        names = [model.id for model in client.models.list().data]
        print(f"Gateway check passed. Models for this key: {', '.join(names) or 'none'}")
    except APIStatusError as exc:
        status, code, message = error_parts(exc)
        print(f"Gateway check failed: HTTP {status} {code} {message}")
    except APIConnectionError as exc:
        print(f"Gateway check failed: {exc.__cause__ or exc}")

    Handler.app = app
    ThreadingHTTPServer.daemon_threads = True
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print(f"Cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1

    if args.host in ("0.0.0.0", "::"):
        shown = address_towards(app.gateway_host) or "<this-server>"
        print(f"Open http://{shown}:{args.port}  (listening on every interface, no login)")
    else:
        print(f"Open http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0


PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gateway test console</title>
<style>
:root {
  --paper: #eef1f4; --surface: #ffffff; --ink: #18222d; --muted: #586675;
  --rule: #c9d2db; --steel: #2b5a85; --pass: #1a7556; --stop: #b93227; --tool: #7d5208;
  --code-bg: #e4e9ee;
  color-scheme: light dark;
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper: #121920; --surface: #1a232c; --ink: #e6ebf0; --muted: #98a6b3;
    --rule: #33414e; --steel: #84b6e0; --pass: #5ccaa2; --stop: #f4857a; --tool: #e2b862;
    --code-bg: #232e39;
  }
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0; background: var(--paper); color: var(--ink);
  font: 1rem/1.5 "Segoe UI Variable Text", "Segoe UI", "SF Pro Text", "Helvetica Neue", system-ui, sans-serif;
  display: flex; flex-direction: column;
}
button, select, textarea, input { font: inherit; color: inherit; }
:focus-visible { outline: 2px solid var(--steel); outline-offset: 2px; }
code, pre, .mono { font-family: ui-monospace, "Cascadia Mono", "SF Mono", Menlo, Consolas, monospace; font-size: 0.875em; }

header {
  background: var(--surface); border-bottom: 1px solid var(--rule);
  padding: 0.75rem 1.25rem; display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem 1.25rem;
}
h1 { font-size: 1.125rem; font-weight: 650; margin: 0 auto 0 0; letter-spacing: -0.01em; }
.control { display: flex; align-items: center; gap: 0.4rem; font-size: 0.9375rem; }
select {
  background: var(--surface); border: 1px solid var(--rule); border-radius: 4px; padding: 0.25rem 0.4rem; max-width: 16rem;
}
input[type=checkbox] { width: 1rem; height: 1rem; accent-color: var(--steel); margin: 0; }
.plain {
  background: none; border: 1px solid var(--rule); border-radius: 4px; padding: 0.3rem 0.7rem; cursor: pointer;
}
.plain:hover { border-color: var(--steel); }
details.system { flex-basis: 100%; font-size: 0.9375rem; }
details.system summary { cursor: pointer; color: var(--muted); width: max-content; }
details.system textarea { display: block; width: min(100%, 46rem); margin-top: 0.4rem; min-height: 4.5rem; }
textarea {
  background: var(--surface); border: 1px solid var(--rule); border-radius: 4px; padding: 0.55rem 0.7rem; resize: vertical;
}

main { flex: 1; overflow-y: auto; padding: 1.5rem 1.25rem 2rem; }
.column { max-width: 46rem; margin: 0 auto; }

.empty h2 { font-size: 1.75rem; line-height: 1.2; font-weight: 650; letter-spacing: -0.02em; margin: 1.5rem 0 0.5rem; }
.empty p { color: var(--muted); margin: 0 0 1.25rem; max-width: 38rem; }
.presets { list-style: none; padding: 0; margin: 0; border-top: 1px solid var(--rule); }
.presets li { border-bottom: 1px solid var(--rule); }
.presets button {
  display: grid; grid-template-columns: 13rem 1fr; gap: 1rem; width: 100%; text-align: left;
  background: none; border: 0; padding: 0.7rem 0.25rem; cursor: pointer;
}
.presets button:hover { background: var(--surface); }
.presets .what { font-weight: 600; }
.presets .why { color: var(--muted); }
details.tests { margin: 0 0 0.6rem; font-size: 0.9375rem; }
details.tests summary { cursor: pointer; color: var(--muted); width: max-content; padding: 0.2rem 0; }
details.tests[open] summary { margin-bottom: 0.2rem; }
details.tests .presets { max-height: 40vh; overflow-y: auto; }

.turn { margin-bottom: 2rem; }
.prompt { font-weight: 600; white-space: pre-wrap; overflow-wrap: anywhere; margin: 0; }
.prompt::before { content: "You asked"; display: block; font-weight: 400; font-size: 0.8125rem; color: var(--muted); }

/* The path each request took: this agent, the gateway, the model. */
.trace { margin: 0.9rem 0 0.75rem; }
.trace-line { display: flex; align-items: center; }
.dot { width: 0.75rem; height: 0.75rem; flex: none; border-radius: 50%; border: 2px solid var(--muted); background: var(--paper); }
.seg { flex: 1; height: 2px; background: var(--rule); position: relative; overflow: hidden; }
.seg::after { content: ""; position: absolute; inset: 0 auto 0 0; width: 35%; background: var(--steel); display: none; }
.trace-labels { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0.5rem; margin-top: 0.3rem; font-size: 0.8125rem; color: var(--muted); }
.trace-labels span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.trace-labels span:nth-child(2) { text-align: center; }
.trace-labels span:nth-child(3) { text-align: right; }
.outcome { margin: 0.5rem 0 0; font-size: 0.875rem; color: var(--muted); }
.outcome strong { color: var(--ink); }

.trace .d-agent { background: var(--ink); border-color: var(--ink); }
@keyframes travel { from { transform: translateX(-100%); } to { transform: translateX(290%); } }
.trace[data-state=pending] .s1::after,
.trace[data-state=replying] .s2::after { display: block; animation: travel 1.1s linear infinite; }
.trace[data-state=replying] .s1, .trace[data-state=passed] .s1, .trace[data-state=passed] .s2,
.trace[data-state=refused] .s1 { background: var(--steel); }
.trace[data-state=replying] .d-gw { background: var(--steel); border-color: var(--steel); }
.trace[data-state=passed] .d-gw { background: var(--pass); border-color: var(--pass); }
.trace[data-state=passed] .d-model { background: var(--steel); border-color: var(--steel); }
.trace[data-state=passed] .outcome strong { color: var(--pass); }
.trace[data-state=refused] .d-gw { background: var(--stop); border-color: var(--stop); border-radius: 2px; }
.trace[data-state=refused] .s2, .trace[data-state=unreachable] .s1 {
  background: repeating-linear-gradient(90deg, var(--rule) 0 6px, transparent 6px 12px);
}
.trace[data-state=unreachable] .d-gw { border-color: var(--stop); border-radius: 2px; }
.trace[data-state=refused] .outcome strong, .trace[data-state=unreachable] .outcome strong { color: var(--stop); }
@media (prefers-reduced-motion: reduce) {
  .trace[data-state=pending] .s1::after, .trace[data-state=replying] .s2::after { animation: none; width: 100%; opacity: 0.45; }
}

.tool { border-left: 3px solid var(--tool); padding: 0.1rem 0 0.1rem 0.7rem; margin: 0.5rem 0; font-size: 0.875rem; }
.tool summary { cursor: pointer; overflow-wrap: anywhere; }
.tool summary b { color: var(--tool); font-weight: 600; }
.tool pre { margin: 0.4rem 0 0.2rem; }
pre { background: var(--code-bg); border-radius: 4px; padding: 0.6rem 0.75rem; overflow-x: auto; white-space: pre; }
.reply .prose { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0.5rem 0; }
.reply .prose code { background: var(--code-bg); border-radius: 3px; padding: 0.05em 0.3em; }
.problem { border-left: 3px solid var(--stop); padding: 0.1rem 0 0.1rem 0.7rem; margin: 0.6rem 0; }
.problem p { margin: 0.2rem 0; overflow-wrap: anywhere; }
.problem .note { color: var(--muted); font-size: 0.875rem; }

footer { background: var(--surface); border-top: 1px solid var(--rule); padding: 0.75rem 1.25rem 1rem; }
.composer { max-width: 46rem; margin: 0 auto; display: flex; gap: 0.6rem; align-items: flex-end; }
.composer textarea { flex: 1; min-height: 2.75rem; max-height: 14rem; resize: none; }
.send {
  background: var(--steel); color: var(--surface); border: 1px solid var(--steel); border-radius: 4px;
  padding: 0.55rem 1.1rem; font-weight: 600; cursor: pointer;
}
.send:disabled { opacity: 0.5; cursor: default; }
.hint { max-width: 46rem; margin: 0.4rem auto 0; font-size: 0.8125rem; color: var(--muted); }
[hidden] { display: none !important; }
@media (max-width: 34rem) {
  .presets button { grid-template-columns: 1fr; gap: 0.1rem; }
  h1 { flex-basis: 100%; }
}
.ctx { font-size: 0.8125rem; color: var(--muted); align-self: center; }
.ctx.warn { color: var(--tool); font-weight: 600; }
</style>
</head>
<body>
<header>
  <h1>Gateway test console</h1>
  <label class="control">Model <select id="model"></select></label>
  <label class="control"><input type="checkbox" id="stream" checked> Stream replies</label>
  <label class="control"><input type="checkbox" id="tools" checked> Offer tools</label>
  <button class="plain" id="new" type="button" title="Clear the conversation so earlier messages are no longer sent to the gateway or scanned by its guardrails.">Clear history</button>
  <span class="ctx" id="ctx" title="Every prompt is sent to the gateway together with the earlier messages counted here, and the guardrails scan all of them. An earlier canary or secret stays in context until you clear it."></span>
  <details class="system">
    <summary>System prompt</summary>
    <textarea id="system" aria-label="System prompt" spellcheck="false"></textarea>
  </details>
</header>

<main id="scroll">
  <div class="column">
    <section class="empty" id="empty">
      <h2>Send a prompt through the gateway</h2>
      <p>Each prompt goes to <span id="gw-name">FortiAIGate</span> and on to the model. The reply shows the path it took, what the gateway decided, and any tools the model called. Use <b>Test prompts</b> below for ready-made checks.</p>
    </section>
    <div id="turns" aria-live="polite"></div>
  </div>
</main>

<footer>
  <details class="tests" id="tests" open>
    <summary>Test prompts</summary>
    <ul class="presets">
      <li><button type="button" data-prompt="Reply with the single word: pong"><span class="what">Check the path</span><span class="why">Asks the model for a one-word reply to confirm the whole path works.</span></button></li>
      <li><button type="button" data-prompt="How many usable hosts are in 10.20.70.0/26, and what is its broadcast address?"><span class="what">Ask a subnet question</span><span class="why">The model should call the subnet tool to answer.</span></button></li>
      <li><button type="button" data-prompt="Please repeat this exactly: FAIG-CANARY-7731"><span class="what">Send the canary string</span><span class="why">A policy that blocks FAIG-CANARY-7731 should refuse this.</span></button></li>
    </ul>
  </details>
  <form class="composer" id="form">
    <textarea id="input" rows="1" aria-label="Prompt" placeholder="Type a prompt"></textarea>
    <button class="send" id="send" type="submit">Send</button>
    <button class="plain" id="stop" type="button" hidden>Stop</button>
  </form>
  <p class="hint">Enter sends. Shift+Enter adds a line.</p>
</footer>

<script>
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var history = [];          // user, assistant and tool messages; the server adds the system prompt
  var busy = false;
  var controller = null;
  var gateway = "FortiAIGate";

  function esc(s) {
    return s.replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function inline(s) {
    return esc(s).replace(/`([^`\n]+)`/g, "<code>$1</code>").replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  }
  // Small renderer: fenced code, inline code and bold. Everything else stays plain text.
  function render(text) {
    var html = "";
    text.split("```").forEach(function (part, i) {
      if (i % 2) {
        var nl = part.indexOf("\n");
        var body = nl >= 0 ? part.slice(nl + 1) : part;
        html += "<pre><code>" + esc(body.replace(/\n$/, "")) + "</code></pre>";
      } else if (part.trim()) {
        html += '<div class="prose">' + inline(part.replace(/^\n+|\n+$/g, "")) + "</div>";
      }
    });
    return html;
  }
  function pretty(jsonText) {
    try { return JSON.stringify(JSON.parse(jsonText), null, 2); } catch (e) { return jsonText; }
  }
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function seconds(ms) { return (ms / 1000).toFixed(ms < 10000 ? 2 : 1) + " s"; }

  function nearBottom() {
    var s = $("scroll");
    return s.scrollHeight - s.scrollTop - s.clientHeight < 120;
  }
  function scrollDown(force) {
    var s = $("scroll");
    if (force || nearBottom()) s.scrollTop = s.scrollHeight;
  }

  function newTurn(prompt, model) {
    var turn = el("article", "turn");
    turn.appendChild(el("p", "prompt", prompt));

    var trace = el("div", "trace");
    trace.dataset.state = "pending";
    var line = el("div", "trace-line");
    ["dot d-agent", "seg s1", "dot d-gw", "seg s2", "dot d-model"].forEach(function (cls) { line.appendChild(el("span", cls)); });
    var labels = el("div", "trace-labels");
    labels.appendChild(el("span", "", "This agent"));
    labels.appendChild(el("span", "", gateway));
    var modelLabel = el("span", "", model);
    labels.appendChild(modelLabel);
    var outcome = el("p", "outcome", "Waiting for the gateway.");
    trace.appendChild(line); trace.appendChild(labels); trace.appendChild(outcome);
    turn.appendChild(trace);

    var work = el("div", "work");
    var reply = el("div", "reply");
    turn.appendChild(work); turn.appendChild(reply);
    $("turns").appendChild(turn);
    return { turn: turn, trace: trace, outcome: outcome, work: work, reply: reply, modelLabel: modelLabel, tools: {} };
  }

  function setOutcome(view, state, strong, rest) {
    view.trace.dataset.state = state;
    view.outcome.textContent = "";
    if (strong) view.outcome.appendChild(el("strong", "", strong));
    view.outcome.appendChild(document.createTextNode((strong ? " " : "") + rest));
  }

  function showProblem(view, lines, note) {
    var box = el("div", "problem");
    lines.forEach(function (text, i) { if (text) box.appendChild(el("p", i ? "mono" : "", text)); });
    box.appendChild(el("p", "note", note || "This turn was not added to the conversation."));
    view.reply.appendChild(box);
  }

  function setBusy(on) {
    busy = on;
    $("send").disabled = on;
    $("stop").hidden = !on;
  }

  async function send(prompt) {
    if (busy || !prompt.trim()) return;
    $("empty").hidden = true;
    var model = $("model").value;
    var view = newTurn(prompt, model);
    scrollDown(true);
    setBusy(true);
    controller = new AbortController();

    var started = performance.now();
    var text = "";
    var requests = 0;
    var finished = false;

    function handle(ev) {
      var stick = nearBottom();
      if (ev.type === "start") {
        view.modelLabel.textContent = ev.model;
      } else if (ev.type === "request") {
        requests = ev.number;
      } else if (ev.type === "text") {
        if (view.trace.dataset.state === "pending") setOutcome(view, "replying", "", "The gateway passed the request. Receiving the reply.");
        text += ev.delta;
        view.reply.innerHTML = render(text);
      } else if (ev.type === "tool_call") {
        if (view.trace.dataset.state === "pending") setOutcome(view, "replying", "", "The gateway passed the request. The model is calling a tool.");
        var box = el("details", "tool");
        var summary = el("summary");
        summary.appendChild(document.createTextNode("Tool call "));
        summary.appendChild(el("b", "mono", ev.name));
        summary.appendChild(document.createTextNode(" "));
        summary.appendChild(el("span", "mono", ev.arguments));
        box.appendChild(summary);
        view.work.appendChild(box);
        view.tools[ev.id] = box;
        text = "";                       // any text before a tool call is replaced by the final answer
        view.reply.innerHTML = "";
      } else if (ev.type === "tool_result") {
        var target = view.tools[ev.id];
        if (target) target.appendChild(el("pre", "", pretty(ev.result)));
      } else if (ev.type === "done") {
        finished = true;
        var took = seconds(performance.now() - started);
        var detail = requests > 1 ? "HTTP 200, " + requests + " requests to the gateway, " + took + "." : "HTTP 200, " + took + ".";
        setOutcome(view, "passed", "Passed by the gateway.", detail);
        if (!text.trim() && !view.reply.innerHTML) view.reply.appendChild(el("p", "outcome", "The model returned an empty reply."));
        history.push({ role: "user", content: prompt });
        ev.messages.forEach(function (m) { history.push(m); });
        updateCtx();
      } else if (ev.type === "error") {
        finished = true;
        if (ev.kind === "gateway") {
          setOutcome(view, "refused", "Refused by the gateway.", "HTTP " + ev.status + (ev.code ? ", " + ev.code : "") + ".");
          showProblem(view, ["The gateway answered with an error instead of a reply.", ev.message]);
        } else if (ev.kind === "connection") {
          setOutcome(view, "unreachable", "Gateway not reached.", "The agent could not connect to " + gateway + ".");
          showProblem(view, [ev.hint || "Check that the gateway is up and that this server can reach it on port 443.", ev.message]);
        } else if (ev.kind === "timeout") {
          setOutcome(view, "unreachable", "No answer in time.", ev.message);
          showProblem(view, ["The gateway accepted the connection but did not finish the reply."]);
        } else {
          setOutcome(view, "stopped", "", "The agent stopped this turn.");
          showProblem(view, [ev.message]);
        }
      }
      if (stick) scrollDown(true);
    }

    try {
      var resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          messages: history.concat([{ role: "user", content: prompt }]),
          model: model,
          stream: $("stream").checked,
          tools: $("tools").checked,
          system: $("system").value
        })
      });
      if (!resp.ok) {
        var problem = "HTTP " + resp.status;
        try { problem = (await resp.json()).error || problem; } catch (e) {}
        handle({ type: "error", kind: "agent", message: problem });
      } else {
        var reader = resp.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";
        for (;;) {
          var chunk = await reader.read();
          if (chunk.done) break;
          buffer += decoder.decode(chunk.value, { stream: true });
          var at;
          while ((at = buffer.indexOf("\n")) >= 0) {
            var row = buffer.slice(0, at);
            buffer = buffer.slice(at + 1);
            if (row.trim()) handle(JSON.parse(row));
          }
        }
        if (!finished) handle({ type: "error", kind: "agent", message: "The connection to the agent closed before the reply finished." });
      }
    } catch (err) {
      if (err.name === "AbortError") {
        setOutcome(view, "stopped", "", "You stopped this request.");
        view.reply.appendChild(el("p", "outcome", "This turn was not added to the conversation."));
      } else if (!finished) {
        setOutcome(view, "stopped", "", "The page lost its connection to the agent.");
        showProblem(view, ["Check that faig_web.py is still running.", String(err.message || err)]);
      }
    } finally {
      controller = null;
      setBusy(false);
      $("input").focus();
    }
  }

  function grow() {
    var box = $("input");
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight + 2, 224) + "px";
  }

  $("form").addEventListener("submit", function (e) {
    e.preventDefault();
    var box = $("input");
    var prompt = box.value;
    if (busy || !prompt.trim()) return;
    box.value = "";
    grow();
    send(prompt);
  });
  $("input").addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      $("form").requestSubmit();
    }
  });
  $("input").addEventListener("input", grow);
  $("stop").addEventListener("click", function () { if (controller) controller.abort(); });
  function updateCtx() {
    var n = history.length;
    var ctx = $("ctx");
    if (!ctx) return;
    ctx.textContent = n ? n + " earlier message" + (n === 1 ? "" : "s") + " in context" : "No earlier messages";
    ctx.className = "ctx" + (n ? " warn" : "");
  }
  $("new").addEventListener("click", function () {
    if (controller) controller.abort();
    history = [];
    $("turns").textContent = "";
    $("empty").hidden = false;
    updateCtx();
    $("input").focus();
  });
  Array.prototype.forEach.call(document.querySelectorAll(".presets button"), function (button) {
    button.addEventListener("click", function () { send(button.dataset.prompt); });
  });

  function fillModels(names, chosen) {
    var select = $("model");
    select.textContent = "";
    names.forEach(function (name) {
      var option = el("option", "", name);
      option.value = name;
      select.appendChild(option);
    });
    if (names.indexOf(chosen) >= 0) select.value = chosen;
  }

  fetch("/api/config").then(function (r) { return r.json(); }).then(function (cfg) {
    gateway = "FortiAIGate " + cfg.gateway;
    $("gw-name").textContent = gateway;
    $("system").value = cfg.system;
    fillModels([cfg.model], cfg.model);
    return fetch("/api/models").then(function (r) { return r.json(); }).then(function (list) {
      var names = list.models.slice();
      if (names.indexOf(cfg.model) < 0) names.unshift(cfg.model);
      fillModels(names, cfg.model);
    });
  }).catch(function () {});
  updateCtx();
  $("input").focus();
})();
</script>
</body>
</html>
'''


if __name__ == "__main__":
    sys.exit(main())
