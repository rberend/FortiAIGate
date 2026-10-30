#!/usr/bin/env python3
"""faig_monitor.py - sample CPU, memory and GPU on the FortiAIGate host.

Run this ON the FortiAIGate server while faig_bench.py drives load from the
agent side. It samples, each interval:
  - host CPU % and memory used (from /proc)
  - each GPU's utilization and memory (nvidia-smi)
  - each FortiAIGate container's CPU % and memory (docker stats)
and writes a tidy CSV you can pivot later, while printing a live one-line
summary. Stdlib only - no venv needed; just python3, docker and nvidia-smi.

    python3 perf/faig_monitor.py --interval 2 --out faig_load.csv
    # Ctrl+C to stop, or --duration 120 to stop automatically.

If 'docker stats' needs root on this host, run with sudo.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone


def read_cpu_sample():
    with open("/proc/stat", encoding="utf-8") as fh:
        parts = [int(x) for x in fh.readline().split()[1:]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)   # idle + iowait
    return sum(parts), idle


def read_mem():
    info = {}
    with open("/proc/meminfo", encoding="utf-8") as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            info[key] = int(rest.split()[0])                # kB
    total = info["MemTotal"] / 1024.0
    avail = info.get("MemAvailable", info.get("MemFree", 0)) / 1024.0
    return total, total - avail                              # MB total, MB used


def read_gpus():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
        )
        gpus = []
        for line in out.stdout.strip().splitlines():
            util, used, total = (x.strip() for x in line.split(","))
            gpus.append((float(util), float(used), float(total)))
        return gpus
    except Exception:
        return []


def _parse_mem(text: str) -> float:
    text = text.strip()
    for unit, mult in (("GiB", 1024), ("GB", 1000), ("MiB", 1), ("MB", 1),
                       ("KiB", 1 / 1024), ("kB", 1 / 1000), ("B", 1 / 1048576)):
        if text.endswith(unit):
            try:
                return float(text[:-len(unit)]) * mult
            except ValueError:
                return 0.0
    return 0.0


def read_containers(only_faig: bool):
    try:
        out = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.Name}};{{.CPUPerc}};{{.MemUsage}}"],
            capture_output=True, text=True, timeout=30,
        )
        rows = []
        for line in out.stdout.strip().splitlines():
            try:
                name, cpu, mem = line.split(";")
            except ValueError:
                continue
            if only_faig and not (name.startswith("faig-") or "fortiaigate" in name):
                continue
            cpu_pct = float(cpu.strip().rstrip("%") or 0)
            mem_used = _parse_mem(mem.split("/")[0])
            rows.append((name, cpu_pct, mem_used))
        return rows
    except FileNotFoundError:
        return []
    except Exception:
        return []


def main() -> int:
    p = argparse.ArgumentParser(description="Sample CPU/memory/GPU on the FortiAIGate host.")
    p.add_argument("--interval", type=float, default=2.0, help="seconds between samples")
    p.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = until Ctrl+C)")
    p.add_argument("--out", default="faig_load.csv", help="CSV output path")
    p.add_argument("--all-containers", action="store_true", help="include non-FortiAIGate containers")
    args = p.parse_args()

    fh = open(args.out, "w", encoding="utf-8")
    fh.write("timestamp,source,cpu_pct,mem_used_mb,mem_total_mb\n")
    print(f"Sampling every {args.interval}s -> {args.out}   (Ctrl+C to stop)")

    prev_total, prev_idle = read_cpu_sample()
    time.sleep(args.interval)
    deadline = time.perf_counter() + args.duration if args.duration else None
    try:
        while True:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            total, idle = read_cpu_sample()
            dt_total, dt_idle = total - prev_total, idle - prev_idle
            host_cpu = (1 - dt_idle / dt_total) * 100 if dt_total else 0.0
            prev_total, prev_idle = total, idle
            mem_total, mem_used = read_mem()
            fh.write(f"{now},host,{host_cpu:.1f},{mem_used:.0f},{mem_total:.0f}\n")

            gpus = read_gpus()
            for i, (util, used, gtotal) in enumerate(gpus):
                fh.write(f"{now},gpu{i},{util:.0f},{used:.0f},{gtotal:.0f}\n")

            containers = read_containers(only_faig=not args.all_containers)
            for name, cpu_pct, mem_used_mb in containers:
                fh.write(f"{now},{name},{cpu_pct:.1f},{mem_used_mb:.0f},\n")
            fh.flush()

            gpu_str = "  ".join(f"gpu{i} {u:.0f}% {m:.0f}MiB" for i, (u, m, _) in enumerate(gpus)) or "no gpu"
            top = sorted(containers, key=lambda r: r[1], reverse=True)[:3]
            top_str = "  ".join(f"{n.replace('faig-', '')} {c:.0f}%" for n, c, _ in top)
            sys.stdout.write(f"\r{now[11:19]}  host {host_cpu:4.0f}% cpu  {mem_used/1024:4.1f}/{mem_total/1024:.1f}GiB  |  {gpu_str}  |  {top_str}        ")
            sys.stdout.flush()

            if deadline and time.perf_counter() >= deadline:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        fh.close()
        print(f"\nSaved {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
