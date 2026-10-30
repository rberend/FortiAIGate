#!/usr/bin/env python3
"""faig_bench.py - a load generator for FortiAIGate (or any OpenAI-compatible endpoint).

Fires concurrent chat-completion requests and reports latency, throughput and
error/verdict breakdowns. Point it at the gateway to measure the whole path, or
straight at Ollama to measure the model alone - the difference is FortiAIGate's
inspection overhead. Pair it with faig_monitor.py on the FortiAIGate host to see
CPU/memory/GPU while the load runs.

Setup (same venv as the agent):
    ~/faig-agent/bin/pip install openai        # already installed for the agent

Examples:
    # 200 requests, 10 in flight, through the gateway:
    FAIG_API_KEY=... FAIG_CA_CERT=~/faig-tls.crt \\
      ~/faig-agent/bin/python perf/faig_bench.py \\
      --base-url https://192.168.1.139/ai/openai/v1 --model ollama-qwen3.5-9b \\
      --concurrency 10 --requests 200

    # same load straight at Ollama (no gateway) to compare:
    ~/faig-agent/bin/python perf/faig_bench.py \\
      --base-url http://192.168.1.135:11434/v1 --model qwen3.5:9b \\
      --concurrency 10 --requests 200

    # hold 20 in flight for 60 seconds, short replies, save per-request CSV:
    ... --concurrency 20 --duration 60 --max-tokens 64 --csv run.csv

Configuration falls back to FAIG_BASE_URL / FAIG_MODEL / FAIG_CA_CERT / FAIG_API_KEY.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import ssl
import statistics
import sys
import time

try:
    import httpx
    from openai import AsyncOpenAI as _AsyncOpenAI
except Exception as exc:  # pragma: no cover
    sys.exit(f"This tool needs the 'openai' and 'httpx' packages ({exc}).")

DEFAULT_PROMPT = "In two sentences, what is a subnet mask and why is it used?"


def make_client(base_url: str, api_key: str, ca_cert: str | None, timeout: float):
    verify: ssl.SSLContext | bool = True
    if ca_cert:
        if not os.path.isfile(ca_cert):
            sys.exit(f"CA certificate not found: {ca_cert}")
        verify = ssl.create_default_context(cafile=ca_cert)
    http_client = httpx.AsyncClient(verify=verify, timeout=timeout)
    return _AsyncOpenAI(base_url=base_url, api_key=api_key, http_client=http_client, max_retries=0)


class Result:
    __slots__ = ("ok", "latency", "ttft", "prompt_tokens", "completion_tokens", "outcome")

    def __init__(self, ok, latency, ttft, pt, ct, outcome):
        self.ok = ok
        self.latency = latency
        self.ttft = ttft
        self.prompt_tokens = pt
        self.completion_tokens = ct
        self.outcome = outcome


async def one_request(client, model, prompt, stream, max_tokens) -> Result:
    messages = [{"role": "user", "content": prompt}]
    kwargs = {"model": model, "messages": messages}
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    start = time.perf_counter()
    ttft = None
    pt = ct = None
    try:
        if stream:
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}
            got_text = False
            stream_obj = await client.chat.completions.create(**kwargs)
            async for chunk in stream_obj:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    if not got_text:
                        ttft = time.perf_counter() - start
                        got_text = True
                if getattr(chunk, "usage", None):
                    pt = chunk.usage.prompt_tokens
                    ct = chunk.usage.completion_tokens
        else:
            resp = await client.chat.completions.create(**kwargs)
            if resp.usage:
                pt, ct = resp.usage.prompt_tokens, resp.usage.completion_tokens
        latency = time.perf_counter() - start
        return Result(True, latency, ttft, pt, ct, "ok")
    except Exception as exc:
        latency = time.perf_counter() - start
        status = getattr(exc, "status_code", None)
        code = ""
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            err = body.get("error", body)
            if isinstance(err, dict):
                code = str(err.get("code") or err.get("type") or "")
        if status:
            outcome = f"HTTP {status}" + (f"/{code}" if code else "")
        else:
            outcome = type(exc).__name__
        return Result(False, latency, None, None, None, outcome)


def percentiles(values, points=(50, 90, 95, 99)):
    if not values:
        return {p: None for p in points}
    ordered = sorted(values)
    out = {}
    for p in points:
        k = (len(ordered) - 1) * (p / 100.0)
        lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
        out[p] = ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)
    return out


async def run(args) -> dict:
    api_key = os.environ.get("FAIG_API_KEY") or getpass.getpass("FortiAIGate virtual key: ").strip()
    if not api_key:
        sys.exit("No API key given.")

    prompts = [DEFAULT_PROMPT]
    if args.prompt:
        prompts = [args.prompt]
    elif args.prompt_file:
        with open(args.prompt_file, encoding="utf-8") as fh:
            prompts = [line.strip() for line in fh if line.strip()] or prompts

    client = make_client(args.base_url, api_key, args.ca_cert, args.timeout)
    results: list[Result] = []
    counter = {"sent": 0}
    deadline = time.perf_counter() + args.duration if args.duration else None
    total = args.requests if not args.duration else None
    stop = asyncio.Event()

    async def worker(wid: int):
        while not stop.is_set():
            if total is not None:
                i = counter["sent"]
                if i >= total:
                    return
                counter["sent"] = i + 1
            elif deadline is not None and time.perf_counter() >= deadline:
                return
            prompt = prompts[(counter["sent"]) % len(prompts)]
            res = await one_request(client, args.model, prompt, args.stream, args.max_tokens)
            results.append(res)
            done = len(results)
            if done % args.progress_every == 0:
                ok = sum(1 for r in results if r.ok)
                sys.stderr.write(f"\r  {done} done, {ok} ok, {done - ok} failed   ")
                sys.stderr.flush()

    wall_start = time.perf_counter()
    workers = [asyncio.create_task(worker(w)) for w in range(args.concurrency)]
    try:
        await asyncio.gather(*workers)
    except KeyboardInterrupt:
        stop.set()
    wall = time.perf_counter() - wall_start
    sys.stderr.write("\n")
    await client.close()

    # Drop warmup requests from the stats (they include model cold-start).
    stats_results = results[args.warmup:] if args.warmup < len(results) else results
    ok = [r for r in stats_results if r.ok]
    failed = [r for r in stats_results if not r.ok]
    lat = [r.latency for r in ok]
    ttft = [r.ttft for r in ok if r.ttft is not None]
    comp_tokens = [r.completion_tokens for r in ok if r.completion_tokens is not None]

    errors: dict[str, int] = {}
    for r in failed:
        errors[r.outcome] = errors.get(r.outcome, 0) + 1

    summary = {
        "base_url": args.base_url,
        "model": args.model,
        "concurrency": args.concurrency,
        "stream": args.stream,
        "max_tokens": args.max_tokens,
        "requests_total": len(results),
        "requests_in_stats": len(stats_results),
        "warmup_dropped": len(results) - len(stats_results),
        "ok": len(ok),
        "failed": len(failed),
        "success_rate": round(len(ok) / len(stats_results), 4) if stats_results else 0,
        "wall_seconds": round(wall, 2),
        "throughput_rps": round(len(stats_results) / wall, 2) if wall else 0,
        "latency_s": {k: (round(v, 3) if v is not None else None)
                      for k, v in percentiles(lat).items()},
        "latency_mean_s": round(statistics.fmean(lat), 3) if lat else None,
        "latency_max_s": round(max(lat), 3) if lat else None,
        "ttft_s": {k: (round(v, 3) if v is not None else None)
                   for k, v in percentiles(ttft).items()} if ttft else None,
        "completion_tokens_total": sum(comp_tokens) if comp_tokens else None,
        "output_tokens_per_s": round(sum(comp_tokens) / wall, 1) if comp_tokens and wall else None,
        "errors": errors,
    }

    if args.csv:
        with open(args.csv, "w", encoding="utf-8") as fh:
            fh.write("index,ok,latency_s,ttft_s,prompt_tokens,completion_tokens,outcome\n")
            for i, r in enumerate(results):
                fh.write(f"{i},{int(r.ok)},{r.latency:.4f},"
                         f"{'' if r.ttft is None else f'{r.ttft:.4f}'},"
                         f"{'' if r.prompt_tokens is None else r.prompt_tokens},"
                         f"{'' if r.completion_tokens is None else r.completion_tokens},{r.outcome}\n")
        print(f"Per-request rows written to {args.csv}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
        print(f"Summary written to {args.json}")

    return summary


def print_summary(s: dict) -> None:
    print("\n=== FortiAIGate load test ===")
    print(f"  endpoint        {s['base_url']}  ({s['model']})")
    print(f"  concurrency     {s['concurrency']}   stream={s['stream']}   max_tokens={s['max_tokens']}")
    print(f"  requests        {s['requests_in_stats']} in stats"
          + (f" (+{s['warmup_dropped']} warmup dropped)" if s['warmup_dropped'] else ""))
    print(f"  ok / failed     {s['ok']} / {s['failed']}   success={s['success_rate'] * 100:.1f}%")
    print(f"  wall / rps      {s['wall_seconds']} s   {s['throughput_rps']} req/s")
    lat = s["latency_s"]
    print(f"  latency (s)     mean {s['latency_mean_s']}  p50 {lat[50]}  p90 {lat[90]}  p95 {lat[95]}  p99 {lat[99]}  max {s['latency_max_s']}")
    if s["ttft_s"]:
        t = s["ttft_s"]
        print(f"  TTFT (s)        p50 {t[50]}  p90 {t[90]}  p95 {t[95]}  p99 {t[99]}")
    if s["output_tokens_per_s"] is not None:
        print(f"  output tokens   {s['completion_tokens_total']} total   {s['output_tokens_per_s']} tok/s (aggregate)")
    if s["errors"]:
        print("  errors          " + ", ".join(f"{k}: {v}" for k, v in sorted(s["errors"].items())))


def main() -> int:
    p = argparse.ArgumentParser(description="Load-test FortiAIGate or any OpenAI-compatible endpoint.")
    p.add_argument("--base-url", default=os.environ.get("FAIG_BASE_URL", "https://192.168.1.139/ai/openai/v1"))
    p.add_argument("--model", default=os.environ.get("FAIG_MODEL", "ollama-qwen3.5-9b"))
    p.add_argument("--ca-cert", default=os.environ.get("FAIG_CA_CERT"), help="gateway tls.crt for its self-signed cert")
    p.add_argument("--concurrency", type=int, default=5, help="requests in flight at once")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--requests", type=int, default=100, help="total requests to send")
    g.add_argument("--duration", type=float, default=0, help="run for this many seconds instead of a fixed count")
    p.add_argument("--prompt", help="the prompt to send (default: a short subnet question)")
    p.add_argument("--prompt-file", help="file with one prompt per line, cycled through")
    p.add_argument("--max-tokens", type=int, default=128, help="cap reply length for consistent load (0 = model default)")
    p.add_argument("--stream", action="store_true", help="stream replies and measure time-to-first-token")
    p.add_argument("--warmup", type=int, default=0, help="drop this many initial requests from the stats")
    p.add_argument("--timeout", type=float, default=180.0, help="seconds per request")
    p.add_argument("--progress-every", type=int, default=10, help="print progress every N requests")
    p.add_argument("--csv", help="write per-request rows to this file")
    p.add_argument("--json", help="write the summary JSON to this file")
    args = p.parse_args()
    if args.max_tokens == 0:
        args.max_tokens = None

    summary = asyncio.run(run(args))
    print_summary(summary)
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
