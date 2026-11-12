# FortiAIGate 8.0.2 lab with Ollama

A runbook and tools for deploying FortiAIGate 8.0.2 in a lab and sending an AI agent's traffic through it to a local LLM served by Ollama, with tools, retrieval over documentation, and web search on the agent side.

```
Browser ──▶ web agent ──HTTPS──▶ FortiAIGate 8.0.2 ──HTTP──▶ Ollama (qwen3.5:9b)
                │                 (GPU guardrails)
                └─ tools: lab tools · search_docs (Qdrant RAG) · web_search / web_fetch (Tavily, SearXNG fallback)
```

The agent never talks to the model directly. FortiAIGate routes, logs and, with a GPU, inspects every request and reply. Tools run on the agent host, and their results reach the model only through the gateway, so retrieved documents and fetched web pages are inspected too.

## What's in this repository

| Path | Contents |
| --- | --- |
| [`docs/runbook.md`](docs/runbook.md) | Install and configuration runbook: Steps 1 to 9 build the chain; Steps 10 to 13 add MCP tools, RAG, performance testing and web search. Starts with the current lab's configuration |
| [`agent/faig_web.py`](agent/faig_web.py) | Web console that sends prompts through the gateway and shows its verdict, timing and each tool call; renders replies as formatted text, including tables, lists and links; optional bypass toggle and built-in jailbreak tests |
| [`agent/faig_agent.py`](agent/faig_agent.py) | Command-line agent, and the core the web console imports (tool loop, date line, context trimming) |
| [`agent/mcp_server.py`](agent/mcp_server.py) | MCP server for the lab tools: `get_current_time`, `calculate`, `subnet_info` |
| [`agent/mcp_bridge.py`](agent/mcp_bridge.py) | MCP client that starts the tool servers and routes the model's tool calls to them |
| [`agent/rag_ingest.py`](agent/rag_ingest.py), [`rag_server.py`](agent/rag_server.py), [`rag_common.py`](agent/rag_common.py) | RAG: index PDFs, Markdown, text and HTML into Qdrant; the `search_docs` tool |
| [`agent/web_server.py`](agent/web_server.py), [`agent/searxng/`](agent/searxng) | Web search: `web_search` (Tavily with an API key, the self-hosted SearXNG container as fallback) and `web_fetch` |
| `agent/mcp.config.*.example.json` | Tool-server configs: lab tools only, plus RAG, plus web |
| [`perf/`](perf) | Load generator (`faig_bench.py`) and gateway resource monitor (`faig_monitor.py`) |
| [`fortianalyzer/`](fortianalyzer) | FortiAnalyzer 8.0.2 custom log parser for FortiAIGate's syslog request logs |

## The model's tools

| Tool | What it does |
| --- | --- |
| `get_current_time` | Current date and time in any IANA time zone |
| `calculate` | Exact arithmetic, parsed safely |
| `subnet_info` | Network, mask, broadcast, host range and count for a CIDR |
| `search_docs` | Semantic search of the indexed docs (FortiSIEM 7.6 plus anything added) |
| `web_search` | Live web and news search through Tavily (free API key) or the self-hosted SearXNG, with automatic fallback |
| `web_fetch` | Downloads one URL and returns the article text without menus, starting at a `#section` if given; refuses private and loopback addresses |

Around every turn the agent also adds today's date to the system prompt, allows up to 8 tool steps before forcing a final answer, trims older tool results so the request fits the model's context window, and flags a reply that was cut off because that window was full. Searches about events (incidents, breaches, attacks, a year) go to news search automatically, and a search that hits a cold SearXNG retries before giving up.

## What you need

- FortiAIGate 8.0.2 release images, the Docker Compose CLI (`deploy-docker-compose.sh` 0.6.2 or newer) and a FortiAIGate license. They are not in this repository; get them from your Fortinet SE.
- Ubuntu 22.04 or later.
- 250 GB of storage, which leaves room for upgrades and logs.
- 64 GB of RAM on the FortiAIGate host (32 GB runs, with a preflight warning).
- One network interface and internet access, for FortiCare and Docker Hub.
- A supported NVIDIA GPU for FortiAIGate's inspection engines (compute capability 8.0 or higher, about 22 GB or more, such as an RTX 3090), and a second GPU for Ollama (8 GB runs a 9B model with a 32K context, using Ollama's 8-bit context cache).
- Optional: a free Tavily API key for reliable web and news search (1,000 searches a month, no card). Without one, search uses the self-hosted SearXNG.

## Deployment options

| Option | Layout |
| --- | --- |
| A | One Ubuntu host with two GPUs runs FortiAIGate and Ollama |
| B | Two Ubuntu hosts, each with a GPU: one runs FortiAIGate, the other Ollama |

The lab runs Option B: two Ubuntu VMs on Proxmox, with an RTX 3090 passed through to FortiAIGate and an RTX 2080 Super to Ollama.

## Models

| Model | Role |
| --- | --- |
| `qwen3.5:9b` | Default chat and tool-calling model (public name `ollama-qwen3.5-9b`) |
| `mimo-v2.6:latest` | Xiaomi MiMo-V2.6-Distill-Qwen-9B: `qwen3.5:9b` further trained for agent tool use; under evaluation as the next default |
| `qwen2.5:7b` | The model the install steps use as their example |
| `nomic-embed-text` | Embeddings for RAG |

Both 9B models load at about 6.2 GB with a 32K context window (flash attention and an 8-bit context cache) and run fully on the 8 GB RTX 2080 Super, one at a time.

## Run the web agent

On the agent host, after the gateway is set up (runbook Steps 1 to 6):

```bash
sudo apt install -y python3-venv
python3 -m venv ~/faig-agent
~/faig-agent/bin/pip install -r agent/requirements.txt
cp agent/mcp.config.web.example.json agent/mcp.config.json     # optional: enable all tool servers
FAIG_CA_CERT=~/faig-tls.crt FAIG_MCP_CONFIG=agent/mcp.config.json \
  ~/faig-agent/bin/python agent/faig_web.py --host 0.0.0.0
```

Add `--direct-url http://localhost:11434/v1` to show a **Bypass gateway** switch that sends prompts straight to Ollama for side-by-side comparison; the console's Test prompts include a set of jailbreak and prompt-injection checks to run both ways. Run it from the repository root, inside `tmux` to keep it running. Paste the FortiAIGate virtual key when asked, then open `http://<agent-host>:8080`. Runbook Step 7 covers the gateway certificate and all agent settings; Steps 10, 11 and 13 set up the tool servers, Qdrant, SearXNG and the optional Tavily key, which goes in `agent/mcp.config.json` (git ignores it).

## Keep secrets out of this repository

Never commit virtual keys, TLS keys, the FortiAIGate `.env` file, license files or recovery archives. The `.gitignore` excludes the common ones, plus the live `agent/mcp.config.json`, `rag_docs/` and `qdrant_storage/`. The web console has no login, so limit port 8080 to trusted addresses.
