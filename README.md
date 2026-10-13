# FortiAIGate 8.0.2 lab with Ollama

A runbook and test tools for deploying FortiAIGate 8.0.2 in a lab and sending an AI agent's traffic through it to a local LLM served by Ollama.

```
Agent ──HTTPS──▶ FortiAIGate 8.0.2 ──HTTP──▶ Ollama (qwen2.5:7b)
```

The agent never talks to the model directly. FortiAIGate routes, logs and, when a GPU is present, inspects every request and reply.

## What's in this repository

| Path | Contents |
| --- | --- |
| [`docs/runbook.md`](docs/runbook.md) | Install and configuration runbook, Steps 1 to 9 |
| [`agent/faig_web.py`](agent/faig_web.py) | Web console that sends prompts through the gateway and shows what it decided |
| [`agent/faig_agent.py`](agent/faig_agent.py) | Command-line agent, and the core the web console imports |
| [`agent/requirements.txt`](agent/requirements.txt) | Python dependency for the agent |

## What you need

- FortiAIGate 8.0.2 (build 48) release images, the Docker Compose CLI (`deploy-docker-compose.sh`) and a FortiAIGate license. They are not in this repository; get them from your Fortinet SE.
- Ubuntu 22.04 or later.
- 250 GB of storage, which leaves room for upgrades and logs.
- 64 GB of RAM with a GPU, or 32 GB without one.
- One network interface and internet access, for FortiCare and Docker Hub.
- A supported NVIDIA GPU for inspection. Without one, FortiAIGate routes and logs traffic but does not inspect it.

## Deployment options

| Option | Layout | Inspection |
| --- | --- | --- |
| A | One Ubuntu host with GPUs runs FortiAIGate and Ollama | Yes |
| B | Two Ubuntu hosts, each with a GPU: one runs FortiAIGate, the other Ollama | Yes |
| C | No GPU: FortiAIGate and Ollama on one or two hosts | No; routing and logging only |

The runbook uses an RTX 3090 and an RTX 2080 Super as its GPU examples. The lab it was written on is Option C: two Ubuntu 22.04 VMs on Proxmox.

## Run the test agent

On a machine that can reach the gateway, after the gateway is set up (runbook Steps 1 to 6):

```bash
sudo apt install -y python3-venv
python3 -m venv ~/faig-agent
~/faig-agent/bin/pip install -r agent/requirements.txt
FAIG_CA_CERT=~/faig-tls.crt ~/faig-agent/bin/python agent/faig_web.py --host 0.0.0.0
```

Paste the FortiAIGate virtual key when asked, then open `http://<agent-host>:8080`. Runbook Step 7 covers the gateway certificate, the settings and the command-line version.

## Keep secrets out of this repository

Never commit virtual keys, TLS keys, the FortiAIGate `.env` file, license files or recovery archives. The `.gitignore` excludes the common ones. The web console has no login, so limit port 8080 to trusted addresses.
