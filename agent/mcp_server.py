#!/usr/bin/env python3
"""mcp_server.py - a FastMCP server exposing the lab's tools over MCP.

This ports the three tools that were built into faig_agent.py (time, calculate,
subnet_info) to a standalone MCP server, so the agent can reach them as an MCP
*client* instead of calling them in-process. It is the first step toward adding
new tools (and a RAG search tool) without touching the agent each time.

The tool names match the inline ones, so when the agent connects to this server
the MCP versions replace the inline tools of the same name - a clean way to see
the "inline -> MCP" migration in the gateway's logs.

Run it directly to smoke-test, or let the agent launch it over stdio via
FAIG_MCP_CONFIG (see mcp.config.example.json):

    pip install mcp
    python mcp_server.py          # serves on stdio; Ctrl+C to stop
"""
from __future__ import annotations

import ast
import datetime as dt
import ipaddress
import operator
from zoneinfo import ZoneInfo

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "The 'mcp' package is missing. Install it with: pip install mcp"
    ) from exc

mcp = FastMCP("lab-tools")


@mcp.tool()
def get_current_time(timezone: str = "UTC") -> dict:
    """Get the current date and time in an IANA time zone (e.g. America/Denver). Defaults to UTC."""
    now = dt.datetime.now(ZoneInfo(timezone))
    return {"timezone": timezone, "iso": now.isoformat(timespec="seconds")}


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _eval_arithmetic(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_arithmetic(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left, right = _eval_arithmetic(node.left), _eval_arithmetic(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ValueError("exponent too large")
        return _BIN_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_arithmetic(node.operand))
    raise ValueError("only numbers and + - * / // % ** ( ) are allowed")


@mcp.tool()
def calculate(expression: str) -> dict:
    """Evaluate an arithmetic expression exactly, using numbers and + - * / // % ** ( )."""
    value = _eval_arithmetic(ast.parse(expression, mode="eval"))
    return {"expression": expression, "result": value}


@mcp.tool()
def subnet_info(cidr: str) -> dict:
    """Describe an IPv4 or IPv6 subnet: network, netmask, broadcast, host range and host count.

    cidr is a subnet in CIDR notation, for example 10.20.70.0/24.
    """
    net = ipaddress.ip_network(cidr, strict=False)
    usable = net.num_addresses - 2 if net.version == 4 and net.prefixlen < 31 else net.num_addresses
    info = {
        "network": str(net.network_address),
        "prefix_length": net.prefixlen,
        "netmask": str(net.netmask),
        "total_addresses": net.num_addresses,
        "usable_hosts": usable,
        "is_private": net.is_private,
    }
    if net.version == 4:
        info["broadcast"] = str(net.broadcast_address)
    if net.num_addresses > 2:
        last = net.broadcast_address - 1 if net.version == 4 else net.broadcast_address
        info["first_host"] = str(net.network_address + 1)
        info["last_host"] = str(last)
    return info


if __name__ == "__main__":
    mcp.run()
