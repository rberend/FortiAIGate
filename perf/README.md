# Performance testing

Two tools for load-testing FortiAIGate. See **Step 12** in [`../docs/runbook.md`](../docs/runbook.md) for the full workflow.

| File | Runs on | What it does |
| --- | --- | --- |
| `faig_bench.py` | agent side (any host that can reach the gateway) | Fires concurrent chat-completion requests and reports latency percentiles, time-to-first-token, throughput, output tokens/s, and an error/verdict breakdown. Uses the agent's venv (`openai`). |
| `faig_monitor.py` | the FortiAIGate host | Samples host CPU/memory, each GPU, and each `faig-*` container to a tidy CSV while the load runs. Stdlib only. |

Point `faig_bench.py` at the gateway and then at Ollama directly and compare — the difference is the gateway's inspection overhead. Each script's header has its own usage and options (`--help`).
