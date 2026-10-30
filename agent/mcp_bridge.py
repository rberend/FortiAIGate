#!/usr/bin/env python3
"""mcp_bridge.py - connect the agent to MCP servers and expose their tools.

The MCP Python SDK is asynchronous; faig_agent.py is synchronous. This bridge
runs the MCP client sessions in a background event loop and exposes a small
synchronous API the agent uses: list_schemas(), names() and call().

Only stdio servers are supported for now. Configure them in a JSON file in the
standard Claude-Desktop shape and point FAIG_MCP_CONFIG at it:

    {
      "mcpServers": {
        "lab-tools": { "command": "python", "args": ["agent/mcp_server.py"] }
      }
    }

A command of "python" or "python3" is run with the current interpreter
(sys.executable), so the server shares the agent's virtualenv (where 'mcp' is
installed). Relative args resolve against the directory the agent is launched
from (the repository root).

Design note: the stdio client and ClientSession set up anyio cancel scopes that
must be entered and exited in the same task, so a single long-lived task
(_serve) holds every server open with `async with` until shutdown. Tool calls
run as separate coroutines on the same loop against the already-open sessions.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from contextlib import AsyncExitStack

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "MCP support needs the 'mcp' package. Install it with: pip install mcp"
    ) from exc


def load_config(path: str) -> dict:
    """Read an MCP config file and return its {name: spec} server mapping."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    servers = data.get("mcpServers", data)
    if not isinstance(servers, dict):
        raise ValueError("MCP config must contain an 'mcpServers' object")
    return servers


def _resolve_command(command: str) -> str:
    """Run bare python with the agent's own interpreter so it shares the venv."""
    if command in ("python", "python3"):
        return sys.executable
    return command


def _merged_env(extra: dict | None) -> dict | None:
    if not extra:
        return None
    env = dict(os.environ)
    env.update(extra)
    return env


def _result_to_text(result) -> str:
    """Flatten an MCP tool result into a string the model can read."""
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
        else:
            try:
                parts.append(json.dumps(block.model_dump(mode="json")))
            except Exception:
                parts.append(str(block))
    text = "\n".join(parts).strip()
    if getattr(result, "isError", False):
        return json.dumps({"error": text or "the tool reported an error"})
    return text or "{}"


class MCPBridge:
    """Hold one or more stdio MCP servers open and call their tools synchronously."""

    def __init__(self, servers: dict, ready_timeout: float = 60.0, call_timeout: float = 60.0):
        self._call_timeout = call_timeout
        self._sessions: dict[str, ClientSession] = {}
        self._route: dict[str, tuple[str, str]] = {}   # exposed name -> (server, tool)
        self._schemas: list[dict] = []
        self._errors: list[str] = []
        self._ready = threading.Event()
        self._shutdown: asyncio.Event | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="mcp-bridge", daemon=True)
        self._thread.start()
        self._submit(self._serve(servers))
        if not self._ready.wait(timeout=ready_timeout):
            raise RuntimeError("MCP servers did not become ready in time")

    # -- background event loop --
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _serve(self, servers: dict) -> None:
        self._shutdown = asyncio.Event()
        try:
            async with AsyncExitStack() as stack:
                for name, spec in servers.items():
                    try:
                        params = StdioServerParameters(
                            command=_resolve_command(spec["command"]),
                            args=list(spec.get("args", [])),
                            env=_merged_env(spec.get("env")),
                        )
                        read, write = await stack.enter_async_context(stdio_client(params))
                        session = await stack.enter_async_context(ClientSession(read, write))
                        await session.initialize()
                        listed = await session.list_tools()
                        self._sessions[name] = session
                        for tool in listed.tools:
                            self._register(name, tool)
                    except Exception as exc:  # one bad server should not sink the rest
                        self._errors.append(f"{name}: {type(exc).__name__}: {exc}")
                self._ready.set()
                await self._shutdown.wait()
        finally:
            self._ready.set()  # unblock __init__ even if setup raised before readiness

    def _register(self, server: str, tool) -> None:
        exposed = tool.name
        if exposed in self._route:                      # name clash across servers
            exposed = f"{server}__{tool.name}"
        self._route[exposed] = (server, tool.name)
        self._schemas.append({
            "type": "function",
            "function": {
                "name": exposed,
                "description": tool.description or "",
                "parameters": tool.inputSchema or {"type": "object", "properties": {}},
            },
        })

    # -- synchronous API used by the agent --
    def list_schemas(self) -> list[dict]:
        return [dict(s) for s in self._schemas]

    def names(self) -> set:
        return set(self._route)

    def errors(self) -> list[str]:
        return list(self._errors)

    def call(self, name: str, arguments: dict) -> str:
        server, tool = self._route[name]
        session = self._sessions[server]
        future = self._submit(session.call_tool(tool, arguments or {}))
        result = future.result(timeout=self._call_timeout)
        return _result_to_text(result)

    def close(self) -> None:
        if self._shutdown is not None:
            self._loop.call_soon_threadsafe(self._shutdown.set)
        self._thread.join(timeout=10)
        self._loop.call_soon_threadsafe(self._loop.stop)
