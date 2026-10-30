#!/usr/bin/env python3
"""faig_agent.py - a small chat agent that reaches an LLM through FortiAIGate.

The agent speaks the OpenAI chat-completions API to the gateway and never
contacts the model server directly. It supports streaming replies and tool
calling, and it reports gateway refusals (for example a guardrail block)
with their HTTP status and error code.

This file is both the command-line agent and the core that faig_web.py
(the web interface) imports, so keep the two files in the same directory.

Setup (once):
    python3 -m venv ~/faig-agent && ~/faig-agent/bin/pip install openai

Configuration (environment variables):
    FAIG_BASE_URL   Gateway base URL.   Default: https://10.20.70.4/ai/openai/v1
    FAIG_MODEL      Public model name.  Default: ollama-qwen3.5-9b
    FAIG_CA_CERT    Path to the gateway's tls.crt, for its self-signed certificate.
    FAIG_API_KEY    Virtual key. If unset, the agent prompts for it without echo.

Run:
    ~/faig-agent/bin/python faig_agent.py                 # interactive chat
    ~/faig-agent/bin/python faig_agent.py -p "ping"       # one prompt, then exit
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import getpass
import ipaddress
import json
import operator
import os
import ssl
import sys
from zoneinfo import ZoneInfo

try:
    from openai import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        DefaultHttpxClient,
        OpenAI,
    )
except ImportError:
    sys.exit("The 'openai' package is missing. Install it with: pip install openai")

DEFAULT_BASE_URL = "https://10.20.70.4/ai/openai/v1"
DEFAULT_MODEL = "ollama-qwen3.5-9b"
DEFAULT_SYSTEM = (
    "You are a concise assistant for a network security lab. "
    "Use a tool when it gives a more reliable answer than you can give yourself."
)
MAX_TOOL_STEPS = 6


# --------------------------------------------------------------------------
# Tools the model may call. All three are local and have no side effects.
# --------------------------------------------------------------------------

def tool_get_current_time(timezone: str = "UTC") -> dict:
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


def tool_calculate(expression: str) -> dict:
    value = _eval_arithmetic(ast.parse(expression, mode="eval"))
    return {"expression": expression, "result": value}


def tool_subnet_info(cidr: str) -> dict:
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


TOOL_FUNCTIONS = {
    "get_current_time": tool_get_current_time,
    "calculate": tool_calculate,
    "subnet_info": tool_subnet_info,
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Get the current date and time in an IANA time zone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "IANA time zone name, for example America/Denver. Defaults to UTC.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Evaluate an arithmetic expression exactly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Arithmetic using numbers and + - * / // % ** ( ), for example (1500-20-8)*3.",
                    }
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "subnet_info",
            "description": "Describe an IPv4 or IPv6 subnet: network, netmask, broadcast, host range and host count.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cidr": {
                        "type": "string",
                        "description": "Subnet in CIDR notation, for example 10.20.70.0/24.",
                    }
                },
                "required": ["cidr"],
            },
        },
    },
]


def run_tool(name: str, arguments_json: str) -> str:
    """Run one tool call and return its result as a JSON string."""
    try:
        args = json.loads(arguments_json) if arguments_json.strip() else {}
        if not isinstance(args, dict):
            raise ValueError("tool arguments must be a JSON object")
        func = TOOL_FUNCTIONS.get(name)
        if func is None:
            raise ValueError(f"unknown tool: {name}")
        result = func(**args)
    except Exception as exc:  # the error goes back to the model, which can retry
        result = {"error": f"{type(exc).__name__}: {exc}"}
    return json.dumps(result)


# --------------------------------------------------------------------------
# Agent core, shared by the command line below and by faig_web.py
# --------------------------------------------------------------------------

class ToolLoopError(RuntimeError):
    """The model kept calling tools without giving a final answer."""


def error_parts(exc: APIStatusError) -> tuple[int, str, str]:
    """Return (HTTP status, error code, message) for a non-2xx gateway response."""
    code, message = "", ""
    body = exc.body
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict):
            code = str(err.get("code") or err.get("type") or "")
            message = str(err.get("message") or "")
        else:
            message = str(err)
    if not message:
        try:
            message = (exc.response.text or "").strip()[:500]
        except Exception:
            message = ""
    return exc.status_code, code, message or exc.message


def describe_status_error(exc: APIStatusError) -> str:
    """Summarize a non-2xx gateway response on one line."""
    status, code, message = error_parts(exc)
    detail = f"{code}: {message}" if code else message
    return f"[gateway HTTP {status}] {detail}"


def build_client(base_url: str, api_key: str, ca_cert: str | None, timeout: float) -> OpenAI:
    verify: ssl.SSLContext | bool = True
    if ca_cert:
        if not os.path.isfile(ca_cert):
            sys.exit(f"CA certificate not found: {ca_cert}")
        verify = ssl.create_default_context(cafile=ca_cert)
    http_client = DefaultHttpxClient(verify=verify, timeout=timeout)
    # No automatic retries: one prompt is one request in the gateway's Traffic Log.
    return OpenAI(base_url=base_url, api_key=api_key, http_client=http_client, max_retries=0)


def _tool_call_dict(call_id: str, name: str, arguments: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments or "{}"}}


def _single_completion(client: OpenAI, kwargs: dict):
    response = client.chat.completions.create(**kwargs)
    choice = response.choices[0].message
    message: dict = {"role": "assistant", "content": choice.content or ""}
    if choice.content:
        yield {"type": "text", "delta": choice.content}
    if choice.tool_calls:
        message["tool_calls"] = [
            _tool_call_dict(call.id or f"call_{index}", call.function.name, call.function.arguments)
            for index, call in enumerate(choice.tool_calls)
        ]
    if response.usage:
        yield {
            "type": "usage",
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
        }
    return message


def _stream_completion(client: OpenAI, kwargs: dict):
    text_parts: list[str] = []
    calls: dict[int, dict] = {}
    stream = client.chat.completions.create(stream=True, **kwargs)
    try:
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            if delta.content:
                text_parts.append(delta.content)
                yield {"type": "text", "delta": delta.content}
            for piece in delta.tool_calls or []:
                key = piece.index if piece.index is not None else len(calls)
                slot = calls.get(key)
                # Some servers send several complete calls that all carry index 0.
                if slot and piece.id and slot["id"] and piece.id != slot["id"]:
                    key = max(calls) + 1
                    slot = None
                if slot is None:
                    slot = calls[key] = {"id": "", "name": "", "arguments": ""}
                if piece.id:
                    slot["id"] = piece.id
                if piece.function:
                    if piece.function.name and not slot["name"]:
                        slot["name"] = piece.function.name
                    if piece.function.arguments:
                        slot["arguments"] += piece.function.arguments
    finally:
        stream.close()  # also stops the upstream request if the caller gives up early
    message: dict = {"role": "assistant", "content": "".join(text_parts)}
    if calls:
        message["tool_calls"] = [
            _tool_call_dict(slot["id"] or f"call_{key}", slot["name"], slot["arguments"])
            for key, slot in sorted(calls.items())
        ]
    return message


def run_turn(client: OpenAI, model: str, history: list[dict], *, stream: bool = True, tools: bool = True):
    """Run one user turn: call the model, run any tool calls, repeat until it answers.

    `history` must already end with the user's message. This is a generator of
    event dictionaries:
        {"type": "request", "number": n}                      a request to the gateway starts
        {"type": "text", "delta": str}                        part of the reply
        {"type": "tool_call", "id", "name", "arguments"}      the model asked for a tool
        {"type": "tool_result", "id", "name", "arguments", "result"}
        {"type": "usage", "prompt_tokens", "completion_tokens"}   non-streaming only
    Its return value (StopIteration.value) is the list of new assistant and tool
    messages to append to the history. Gateway and connection errors propagate
    as openai exceptions; nothing is added to the history in that case.
    """
    new: list[dict] = []
    for number in range(1, MAX_TOOL_STEPS + 1):
        kwargs: dict = {"model": model, "messages": history + new}
        if tools:
            kwargs["tools"] = TOOL_SCHEMAS
        yield {"type": "request", "number": number}
        complete = _stream_completion if stream else _single_completion
        message = yield from complete(client, kwargs)
        new.append(message)
        calls = message.get("tool_calls")
        if not calls:
            return new
        for call in calls:
            name = call["function"]["name"]
            arguments = call["function"]["arguments"]
            yield {"type": "tool_call", "id": call["id"], "name": name, "arguments": arguments}
            result = run_tool(name, arguments)
            yield {"type": "tool_result", "id": call["id"], "name": name, "arguments": arguments, "result": result}
            new.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    raise ToolLoopError(f"stopped after {MAX_TOOL_STEPS} tool steps without a final answer")


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

class Agent:
    def __init__(self, client: OpenAI, model: str, system: str, stream: bool, tools: bool, verbose: bool):
        self.client = client
        self.model = model
        self.system = system
        self.stream = stream
        self.tools = tools
        self.verbose = verbose
        self.messages: list[dict] = []
        self.reset()

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": self.system}]

    def ask(self, text: str) -> bool:
        """Send one user turn and print the answer. Returns True on success.

        On a gateway or connection error the whole turn is removed from the
        history, so a blocked prompt is not resent with every later request.
        """
        checkpoint = len(self.messages)
        self.messages.append({"role": "user", "content": text})
        line_open = False
        try:
            events = run_turn(self.client, self.model, self.messages, stream=self.stream, tools=self.tools)
            while True:
                try:
                    event = next(events)
                except StopIteration as stop:
                    new_messages = stop.value
                    break
                kind = event["type"]
                if kind == "text":
                    print(event["delta"], end="", flush=True)
                    line_open = True
                    continue
                if line_open:
                    print()
                    line_open = False
                if kind == "tool_result":
                    print(f"  [tool] {event['name']}({event['arguments']}) -> {event['result']}")
                elif kind == "usage" and self.verbose:
                    print(f"  [usage] prompt={event['prompt_tokens']} completion={event['completion_tokens']}")
            if line_open:
                print()
            self.messages.extend(new_messages)
            return True
        except ToolLoopError as exc:
            message = f"[{exc}]"
        except APIStatusError as exc:
            message = describe_status_error(exc)
        except APITimeoutError:
            message = "[timeout] The gateway did not answer in time."
        except APIConnectionError as exc:
            cause = exc.__cause__ or exc
            message = f"[connection error] {cause}"
            if "CERTIFICATE_VERIFY_FAILED" in str(cause):
                message += "\n  Set FAIG_CA_CERT to the gateway's tls.crt."
        if line_open:
            print()
        print(message)
        del self.messages[checkpoint:]
        print("  (this turn was removed from the conversation history)")
        return False


HELP = """Commands:
  /help              show this help
  /models            list the models this key may call
  /model NAME        switch model
  /stream on|off     streaming replies
  /tools on|off      offer the tools to the model
  /system TEXT       replace the system prompt and reset the conversation
  /reset             clear the conversation
  /quit              exit"""


def list_models(client: OpenAI) -> None:
    try:
        models = client.models.list()
    except APIStatusError as exc:
        print(describe_status_error(exc))
        return
    except APIConnectionError as exc:
        print(f"[connection error] {exc.__cause__ or exc}")
        return
    names = [model.id for model in models.data]
    print("\n".join(f"  {name}" for name in names) if names else "  (this key may call no models)")


def on_off(value: str, current: bool) -> bool:
    value = value.strip().lower()
    if value in ("on", "off"):
        return value == "on"
    print("  Use 'on' or 'off'.")
    return current


def interactive(agent: Agent) -> None:
    print(f"FortiAIGate agent. Model: {agent.model}. Type /help for commands, /quit to exit.")
    while True:
        try:
            line = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line.startswith("/"):
            command, _, rest = line.partition(" ")
            command = command.lower()
            if command in ("/quit", "/exit"):
                return
            if command == "/help":
                print(HELP)
            elif command == "/models":
                list_models(agent.client)
            elif command == "/model" and rest.strip():
                agent.model = rest.strip()
                print(f"  Model: {agent.model}")
            elif command == "/stream":
                agent.stream = on_off(rest, agent.stream)
                print(f"  Streaming: {'on' if agent.stream else 'off'}")
            elif command == "/tools":
                agent.tools = on_off(rest, agent.tools)
                print(f"  Tools: {'on' if agent.tools else 'off'}")
            elif command == "/system" and rest.strip():
                agent.system = rest.strip()
                agent.reset()
                print("  System prompt replaced; conversation cleared.")
            elif command == "/reset":
                agent.reset()
                print("  Conversation cleared.")
            else:
                print("  Unknown command. Type /help.")
            continue
        try:
            agent.ask(line)
        except KeyboardInterrupt:
            print("\n  [interrupted]")


def main() -> int:
    parser = argparse.ArgumentParser(description="Chat with an LLM through FortiAIGate.")
    parser.add_argument("-p", "--prompt", help="send one prompt, print the answer and exit")
    parser.add_argument("--base-url", default=os.environ.get("FAIG_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default=os.environ.get("FAIG_MODEL", DEFAULT_MODEL))
    parser.add_argument("--ca-cert", default=os.environ.get("FAIG_CA_CERT"),
                        help="path to the gateway's tls.crt")
    parser.add_argument("--system", default=DEFAULT_SYSTEM, help="system prompt")
    parser.add_argument("--no-stream", action="store_true", help="wait for the full reply")
    parser.add_argument("--no-tools", action="store_true", help="do not offer tools to the model")
    parser.add_argument("--timeout", type=float, default=180.0, help="seconds per request")
    parser.add_argument("-v", "--verbose", action="store_true", help="show token usage (non-streaming)")
    args = parser.parse_args()

    api_key = os.environ.get("FAIG_API_KEY") or getpass.getpass("FortiAIGate virtual key: ").strip()
    if not api_key:
        print("No API key given.", file=sys.stderr)
        return 2

    client = build_client(args.base_url, api_key, args.ca_cert, args.timeout)
    agent = Agent(client, args.model, args.system,
                  stream=not args.no_stream, tools=not args.no_tools, verbose=args.verbose)

    if args.prompt is not None:
        return 0 if agent.ask(args.prompt) else 1
    interactive(agent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
