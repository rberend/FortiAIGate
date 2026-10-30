# FortiAIGate 8.0.2 + Ollama Lab Install Runbook

Oct 9, 2026 · @Ray · updated Oct 20, 2026 with the first GPU deployment (Proxmox passthrough, license activation, first-apply recovery)

## Overview

This runbook builds a lab chain in which an agent reaches a local LLM only through FortiAIGate: agent, then FortiAIGate 8.0.2, then Ollama. FortiAIGate is deployed with the Docker Compose CLI (`deploy-docker-compose.sh` 0.6.2), and Ollama serves `qwen2.5:7b`.

Steps 1 to 9 run in order. Each step names the host it applies to, so the same runbook covers all three layouts below.

## Lab as built

The lab is Option C on two VMs with no GPU. A chat request works end to end and the gateway logs it, but nothing is inspected, because security scanning is paused without a GPU.

| Item | Value on 9 October 2026 |
| --- | --- |
| FortiAIGate host | 10.20.70.4, CLI installed in `/home/faig/FAIG` |
| Ollama host | 10.20.70.5 |
| Provider | `Ollama`, Custom (self-hosted), credential `ollama-remote` |
| Public model name | `ollama-qwen2.5` |
| Agent base URL | `https://10.20.70.4/ai/openai/v1` |
| Virtual key | `lab-agent`, global, service |
| Detector | `lab-secrets-regex`, action Block |
| Guardrail | `lab-baseline`, 3 detectors |
| Policy | `lab-default`, in use by all workspaces |
| Test agent | `faig_web.py` on 10.20.70.5, port 8080 |
| License | 1 GPU license, active until 2027-06-04 |
| GPUs in use | 0 |

| Test | Result |
| --- | --- |
| Chat request with `curl` through the gateway | HTTP 200, reply returned |
| Regex detector in the Playground | Reported successful |
| Canary string through the gateway with the policy in place | Not blocked; Traffic Log verdict was Log |
| Prompt from the web console | HTTP 200 after 41.4 s |

## GPU build as built: Option B on Proxmox, 20 October 2026

The first GPU deployment is Option B: two Ubuntu 26.10 VMs on one Proxmox host, each with a card passed through. FortiAIGate inspects on the 3090; Ollama serves on the 2080 Super. With a license imported, live traffic is inspected, which the Option C lab above could not do.

| Item | Value |
| --- | --- |
| FortiAIGate VM | 3090 passed through, 8 vCPU, 32 GB RAM, ~440 GB disk, Ubuntu Server 26.10, CLI state in `~/faig` |
| Ollama VM | 2080 Super passed through, Ubuntu Server 26.10 |
| FortiAIGate release | 8.0.2-build0055, GPU inspection enabled, one GPU shared by Triton and vLLM, gateway memory 4 GB |
| GPU UUIDs | 3090 `GPU-98fd9d66-...` to FortiAIGate; 2080 Super to Ollama |
| Model loaded during bring-up | `deepseek-r1:7b` on the 2080 Super (4.7 GB) |
| Driver | 580.178.04 in both guests |

Three things differed from this runbook's earlier CPU-only notes, all captured in the steps and appendix below: passing the cards through on Proxmox has its own gotchas (Appendix A); the 32 GB on the FortiAIGate VM is a preflight WARN, not a FAIL, and the stack ran healthy on a single shared card; and the imported license did not take effect until the VM was rebooted, beyond the documented API restart.

## Layout options

Pick one layout before you start. They differ in where Ollama runs, whether a GPU is used, and the firewall rule in Step 6.

```mermaid
flowchart TB
  subgraph A["Option A: single host"]
    direction LR
    agentA["Agent<br/>OpenAI client"] -- "HTTPS 443 /ai" --> gwA
    subgraph A1["FortiAIGate server (also runs Ollama)"]
      gwA["FortiAIGate 8.0.2<br/>RTX 3090: Triton + vLLM"] -- "HTTP 11434" --> olA["Ollama qwen2.5:7b<br/>RTX 2080 Super"]
    end
  end
  subgraph B["Option B: two hosts"]
    direction LR
    agentB["Agent<br/>OpenAI client"] -- "HTTPS 443 /ai" --> gwB
    subgraph B1["FortiAIGate server"]
      gwB["FortiAIGate 8.0.2<br/>RTX 3090: Triton + vLLM"]
    end
    subgraph B2["Ollama server"]
      olB["Ollama qwen2.5:7b<br/>RTX 2080 Super"]
    end
    gwB -- "HTTP 11434" --> olB
  end
  subgraph C["Option C: no GPU"]
    direction LR
    agentC["Agent<br/>OpenAI client"] -- "HTTPS 443 /ai" --> gwC
    subgraph C1["FortiAIGate server"]
      gwC["FortiAIGate 8.0.2<br/>CPU only, no inspection"]
    end
    subgraph C2["FortiAIGate or Ollama server"]
      olC["Ollama qwen2.5:7b<br/>on the CPU"]
    end
    gwC -- "HTTP 11434" --> olC
  end
  A ~~~ B ~~~ C
```

In every layout the agent's only route to the model is the FortiAIGate server. Option B moves Ollama and the 2080 Super to a separate Ollama server, and Option C uses no GPU.

| Item | Option A: single host | Option B: two hosts | Option C: no GPU |
| --- | --- | --- | --- |
| FortiAIGate | FortiAIGate server | FortiAIGate server | FortiAIGate server, CPU-only |
| Ollama | On the FortiAIGate server | Ollama server | On the FortiAIGate server or a separate Ollama server, on the CPU |
| RTX 3090 (24 GB, compute capability 8.6) | FortiAIGate server, selected for GPU inspection | FortiAIGate server, selected for GPU inspection | Not used |
| RTX 2080 Super (8 GB, compute capability 7.5) | FortiAIGate server, pinned to Ollama | Ollama server, used by Ollama | Not used |
| NVIDIA driver | FortiAIGate server | Both servers | Not needed |
| Docker and NVIDIA container toolkit | FortiAIGate server | FortiAIGate server only | Docker on the FortiAIGate server, no toolkit |
| Who may reach Ollama on port 11434 | Docker networks on the FortiAIGate server | The FortiAIGate server's IP address | As Option A on one server, as Option B on two |
| Guardrails available | Regular-expression, DLP and natural-language policy | Regular-expression, DLP and natural-language policy | None; scanning is paused without a GPU |

The 3090 goes to FortiAIGate because its policy model (vLLM) refuses a card below compute capability 8.0 and warns below about 22 GB. The 2080 Super goes to Ollama because `qwen2.5:7b` is a 4.7 GB model.

Choose Option B to make the gateway the only network path to the model, and to keep Ollama's GPU and memory use off the FortiAIGate host. If the two systems are VMs on one Proxmox host, pass each card through to its VM first (Appendix A); the Step 2 driver steps inside the guest are then the same.

Choose Option C when no NVIDIA card is available. FortiAIGate then runs CPU-only. The live 8.0.2 WebUI pauses security scanning in this mode and passes traffic uninspected, so the gateway routes, meters and logs requests but blocks nothing. Ollama also answers more slowly on the CPU.

Without GPU inspection, FortiAIGate runs CPU-only and the 3090 can go to Ollama instead. Security scanning is then paused, as in Option C.

## Before you start

The FortiAIGate CLI runs a preflight on every launch and exits on any FAIL, so check these on the FortiAIGate server first.

| Check on the FortiAIGate server | Requirement | If not met |
| --- | --- | --- |
| Operating system | Ubuntu 22.04 or later | Not supported |
| Operator account | Non-root user in an interactive terminal | CLI refuses to start |
| Docker Compose plugin | 2.23.1 or later | FAIL |
| Free space in Docker's data filesystem | 60 GB, 120 GB recommended | FAIL below 60 GB |
| Free space for CLI state | 5 GB, 10 GB recommended | FAIL below 5 GB |
| Outbound access to `fds1.fortinet.com:443` | Reachable | FAIL |
| NVIDIA container runtime | Registered with Docker whenever `nvidia-smi` works | FAIL, even for a CPU-only deployment |
| CPU cores | 8 or more | WARN |
| Memory | 32 GB CPU-only, 64 GB when a GPU is present | WARN |
| Docker Hub | Reachable, or the nginx, PostgreSQL and Redis images preloaded | WARN |

Have these files on the FortiAIGate server before Step 4:

- `deploy-docker-compose.sh` (CLI 0.6.2 or newer; older versions reject current 8.0.2 Triton images)
- The 8.0.2 release archives, named `FAIG_<component>-V8.0.2-build<N>-FORTINET.tar` or `.tar.gz`
- The FortiAIGate `.lic` license file

The Ollama server in Option B needs only Ubuntu, the NVIDIA driver and about 5 GB of disk for the model.

## Step 1: Enable SSH (each host)

Run this at the console of each Ubuntu host. Every later step can then be done over SSH, which is how the FortiAIGate CLI expects to be run.

```bash
sudo apt update && sudo apt install -y openssh-server
sudo systemctl enable --now ssh
sudo ufw allow OpenSSH        # only needed if ufw is enabled
hostname -I                   # the address to connect to
```

From your own machine, `ssh-copy-id <user>@<host-ip>` sets up key login.

Give each host a fixed IP address or a DHCP reservation. Step 6 uses these addresses in firewall rules and in the FortiAIGate provider settings.

## Step 2: Install the NVIDIA driver (each host with a GPU)

Install the 580 driver branch on every host that holds a card; one driver covers both the RTX 3090 and the RTX 2080 Super. FortiAIGate's Triton image is tagged 25.11, and [Triton 25.11](https://docs.nvidia.com/deeplearning/triton-inference-server/archives/triton-inference-server-2670/release-notes/rel-25-11.html) requires driver release 575 or later on these cards.

Option C: skip this step, and leave the NVIDIA driver uninstalled on the FortiAIGate server. The FortiAIGate preflight demands the container toolkit whenever `nvidia-smi` works.

1. Check Secure Boot. If it is enabled, the installer asks for a one-time MOK password, and you must enroll that key on the blue screen at the next reboot or the driver will not load.

   ```bash
   mokutil --sb-state
   ```
2. Apply pending system updates and reboot if the kernel changed. Then see which cards Ubuntu detects and which driver branches it offers.

   ```bash
   sudo apt update && sudo apt install -y ubuntu-drivers-common
   ubuntu-drivers devices
   sudo ubuntu-drivers list
   ```
3. Install the 580 branch and reboot. If `nvidia:580` is not offered, install the highest listed branch that is 575 or later.

   ```bash
   sudo ubuntu-drivers install nvidia:580
   sudo reboot
   ```

   On a host with no desktop, `sudo ubuntu-drivers install --gpgpu nvidia:580-server` installs the compute-only variant instead.
4. Confirm the driver sees each card, and record the UUIDs. FortiAIGate and Ollama are both pinned to cards by UUID.

   ```bash
   nvidia-smi --query-gpu=index,name,uuid,memory.total,compute_cap --format=csv
   ```

   On a host with both example cards the output looks like this (UUIDs shortened, index order may differ):

   ```text
   index, name, uuid, memory.total [MiB], compute_cap
   0, NVIDIA GeForce RTX 3090, GPU-aaaaaaaa-..., 24576 MiB, 8.6
   1, NVIDIA GeForce RTX 2080 SUPER, GPU-bbbbbbbb-..., 8192 MiB, 7.5
   ```

What each example card can run:

| Card | FortiAIGate policy model (vLLM) | FortiAIGate DLP model (Triton) | Ollama `qwen2.5:7b` |
| --- | --- | --- | --- |
| RTX 3090, 24 GB, capability 8.6 | Yes | Yes | Yes |
| RTX 2080 Super, 8 GB, capability 7.5 | No, the CLI requires 8.0 or later | Meets Triton 25.11's 7.5 minimum, untested at 8 GB | Yes |

In the FortiAIGate wizard, select only the 3090. Selecting both cards would give the 2080 Super to Triton and leave no card for Ollama.

## Step 3: Install Docker and the NVIDIA container toolkit (FortiAIGate server)

The FortiAIGate server needs Docker Engine, the Compose plugin and the NVIDIA container toolkit. The Ollama server in Option B needs none of them.

Option C: do items 1 and 2 only.

1. Install Docker Engine and the Compose plugin from Docker's repository.

   ```bash
   sudo apt-get update && sudo apt-get install -y ca-certificates curl
   sudo install -m 0755 -d /etc/apt/keyrings
   sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
   echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
     | sudo tee /etc/apt/sources.list.d/docker.list
   sudo apt-get update
   sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
   sudo usermod -aG docker $USER
   ```
2. Log out and back in so the group change applies, then confirm the Compose version is 2.23.1 or later.

   ```bash
   docker compose version
   ```
3. Install the NVIDIA container toolkit and register it with Docker. The preflight fails without this once `nvidia-smi` works, even for a CPU-only deployment.

   ```bash
   curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
     | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
   curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
     | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
     | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
   sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
   sudo nvidia-ctk runtime configure --runtime=docker
   sudo systemctl restart docker
   ```
4. Confirm a container can see the cards.

   ```bash
   docker run --rm --gpus all ubuntu nvidia-smi
   ```

## Step 4: Install FortiAIGate 8.0.2 (FortiAIGate server)

The CLI loads the release archives itself, generates the Compose project and starts the stack. Do not run `docker load` on the archives by hand.

1. Put the archives in one folder. Options A and B need all ten; Option C needs only the seven core images.

   | Set | Components in the archive names |
   | --- | --- |
   | Core | `api`, `webui`, `llm-gateway`, `logd`, `license_manager`, `guardrail`, `guardrail-scanner` |
   | GPU inspection | `custom-triton`, `faig-models`, `vllm-server` |
2. Install the CLI in its own directory and run the self-test. Always run it from this directory, because its state lives in `faig_deployment_assistant_data` beside the script.

   ```bash
   mkdir -p ~/faig && cp deploy-docker-compose.sh ~/faig/
   chmod 0755 ~/faig/deploy-docker-compose.sh
   cd ~/faig
   ./deploy-docker-compose.sh --self-test     # ends with "Self-test passed: ..."
   ./deploy-docker-compose.sh
   ```
3. Answer the setup wizard, which starts automatically after a clean preflight.

   | Prompt | Answer |
   | --- | --- |
   | Where are the FortiAIGate images? | `1`, then the absolute path of the archive folder, then the 8.0.2 release |
   | HTTPS port | `443` |
   | HTTP redirect port | `8081`, bound to 127.0.0.1 only |
   | Enable GPU content inspection | Yes in Options A and B, No in Option C |
   | GPU numbers | The number shown beside the RTX 3090's UUID, and only that one. Not asked in Option C |
   | LLM gateway memory in GB | `8` |
   | Log retention storage budget in GB | `16` |
   | Deployment namespace | `fortiaigate` |
   | Use a FortiCare proxy | No |

   In Options A and B the summary should then read "GPU placement: one GPU shared by Triton and vLLM". In Option C it reads "Policy model: unavailable (POLICY detectors fail closed)".
4. Deploy. Choose **3. Deploy or reapply Docker Compose**, answer Yes to creating the recovery archive, store the path it prints, then type `APPLY`. On a busy or 32 GB host the first `APPLY` can end with `dependency failed to start: container faig-postgres is unhealthy`: PostgreSQL misses its first-start healthcheck window while the ~45 GB of GPU images (`vllm-server` 20 GB, `faig-models` 15 GB, `custom-triton` 9.6 GB) load, then recovers on its own. This leaves `nginx`, `logd` and `dlp-scanner` in `Created`. It is not a failed deployment; see item 5.
5. Check the result with **4. Inspect or control the deployment**, then **1. Status and health**. In Options A and B the first start takes several minutes while the models load, and the health checks allow up to 30 minutes.

   | What you see | Meaning |
   | --- | --- |
   | `/ui` returns 502 shortly after apply | Still starting; wait and rerun the status check |
   | First `APPLY` ended "faig-postgres is unhealthy" | Run `./compose.sh ps -a`; if postgres now reads `healthy`, re-run `./compose.sh up -d --remove-orphans` from the deployment folder to start the containers left in `Created`. The model-loader and `logd-api-ready` containers staying `Exited (0)` is success, not failure |
   | `/ai/` returns 403 | Expected until a license is imported |
   | `/ai/` returns 404 after the license is imported | Licensed, but no provider or model route exists yet. Expected until Step 6 registers Ollama |
   | Accelerator inventory with "scanning disabled by licence coverage" | Expected until a license is imported |
   | "Accelerator coverage snapshot is not available yet" just after a reboot | The license manager is still computing its first snapshot; it settles once every Triton and vLLM meter endpoint passes |
   | Triton and vLLM report healthy (Options A and B) | GPU inspection is up |
6. Open `https://<faig-ip>/ui`, accept the self-signed certificate warning, and create the administrator account.
7. Sign in, open the Licenses page and upload the `.lic` file.
8. Restart the API so the built-in detectors are created for the new organization, then run **Status and health** again.

   ```bash
   ~/faig/faig_deployment_assistant_data/deployment/compose.sh restart api
   ```

   On the 20 October build the imported license did not activate after `restart api` alone; `/ai/` stayed 403 and licensed functions stayed locked until the VM was rebooted. If the license does not take effect, reboot the FortiAIGate host and recheck **Status and health**.

Store the recovery archive somewhere safe. It holds the deployment secrets and the stable deployment identity, which must never be regenerated.

## Step 5: Install Ollama and the model (FortiAIGate server in Option A, Ollama server in Option B)

Ollama serves `qwen2.5:7b`, a 4.7 GB model with tool-calling support and a 32K context window ([Ollama library](https://ollama.com/library/qwen2.5)). It fits the 2080 Super's 8 GB.

1. Install Ollama.

   ```bash
   curl -fsSL https://ollama.com/install.sh | sh
   ```
2. Open a systemd override for the service.

   ```bash
   sudo systemctl edit ollama
   ```
3. Add these lines. The first makes Ollama listen on the network instead of only on localhost. The second raises the context window from the 4K default, which tool definitions fill quickly. Ollama's default is 4K on cards under 24 GiB.

   ```ini
   [Service]
   Environment="OLLAMA_HOST=0.0.0.0:11434"
   Environment="OLLAMA_CONTEXT_LENGTH=8192"
   ```
4. Option A only: add a third line that pins Ollama to the 2080 Super by its UUID from Step 2. Without it, Ollama can load the model onto the 3090 that FortiAIGate is using.

   ```ini
   Environment="CUDA_VISIBLE_DEVICES=GPU-<uuid-of-the-2080-super>"
   ```
5. Restart Ollama and pull the model.

   ```bash
   sudo systemctl restart ollama
   ollama pull qwen2.5:7b
   ```
6. Confirm the model answers on the OpenAI-compatible endpoint and runs on the intended card.

   ```bash
   curl http://localhost:11434/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"qwen2.5:7b","messages":[{"role":"user","content":"ping"}]}'
   ollama ps        # PROCESSOR should read 100% GPU
   nvidia-smi       # the model's memory should appear on the 2080 Super
   ```

If Ollama has the 3090 to itself, `qwen2.5:14b` is the better choice.

Option C: skip item 4. Ollama runs the model on the CPU, so `ollama ps` reads 100% CPU and replies are much slower. If `qwen2.5:7b` is too slow, `qwen2.5:3b` is a 1.9 GB alternative, released under the Qwen license rather than Apache 2.0.

Ollama unloads an idle model after about five minutes, and the next request waits while it loads again. On a CPU that wait is long; one reply in this lab took 41 s. To keep the model loaded, add `Environment="OLLAMA_KEEP_ALIVE=24h"` to the override and restart Ollama.

## Step 6: Connect FortiAIGate to Ollama

Restrict port 11434 so that only FortiAIGate can reach Ollama, then register Ollama as the provider. Ollama has no authentication of its own, so the firewall rule is what stops clients bypassing the gateway.

| Setting | Option A: single host | Option B: two hosts |
| --- | --- | --- |
| Ollama address for FortiAIGate | `http://<faig-lan-ip>:11434/v1` | `http://<ollama-ip>:11434/v1` |
| Firewall rule goes on | FortiAIGate server | Ollama server |
| Source allowed to reach 11434 | Docker's networks, `172.16.0.0/12` | The FortiAIGate server's IP address |

Option C uses the Option A column on one host and the Option B column on two.

Use the LAN address in Option A, never `localhost` or `127.0.0.1`. FortiAIGate runs in containers, and inside a container those names mean the container itself.

1. Option A: allow Docker's networks and block everyone else. Run on the FortiAIGate server.

   ```bash
   sudo ufw allow OpenSSH
   sudo ufw allow from 172.16.0.0/12 to any port 11434 proto tcp
   sudo ufw deny 11434/tcp
   sudo ufw enable
   ```
2. Option B: allow the FortiAIGate server and block everyone else. Run on the Ollama server. Containers on the FortiAIGate server leave through its own address, so that is the address the Ollama server sees.

   ```bash
   sudo ufw allow OpenSSH
   sudo ufw allow from <faig-ip> to any port 11434 proto tcp
   sudo ufw deny 11434/tcp
   sudo ufw enable
   ```
3. Test from a container on the FortiAIGate server; it should list `qwen2.5:7b`. Then run the same `curl` from any other machine; it should fail.

   ```bash
   docker run --rm curlimages/curl -s http://<ollama-ip>:11434/v1/models
   ```

Then register Ollama and issue a key in the FortiAIGate WebUI. These pages were read on the live 8.0.2 system on 9 October 2026. 8.0.2 has no AI Guard or AI Flow pages; providers live under **Models** and client keys under **Virtual Keys**.

1. Open **Models**, click **Add Provider**, and choose **Custom (self-hosted)**. The OpenAI entry has a fixed catalog and no base URL field, so it cannot point at Ollama.
2. Fill in Integration Details.

   | Field | Value |
   | --- | --- |
   | Provider Name | `Ollama` |
   | Provider Slug | `ollama`. It prefixes every model's public name and cannot be changed later |
   | Base URL | The Ollama address from the table above, starting with `http://`. Ollama serves plain HTTP on 11434 |
   | API Format | OpenAI-compatible |
3. Click **Add Credential**. The form requires one even though Ollama does not check keys, so give it a name and a placeholder API key, and leave Weight at `1`.
4. Click **Add Model**. Set Model ID to the name exactly as `ollama list` shows it, such as `qwen2.5:7b`, and enter a price; the dialog requires at least one cost, and `0.01` is accepted. Click **Test Connectivity**, then **Save**, then **Add Integration**. The connectivity test has about a 10-second deadline and posts to the model, so a cold model load can fail it with `context deadline exceeded` even when the path is fine (a plain `curl` from the FortiAIGate server to `http://<ollama-ip>:11434/v1/models` still returns 200). Pre-warm the model with one request and set `OLLAMA_KEEP_ALIVE` (Step 5) before testing, then retry.
5. Open **Virtual Keys** and click **Create Virtual Key**.

   | Setting | Value |
   | --- | --- |
   | Scope | Global, unless you have created a workspace |
   | Key Type | Service |
   | Model Access | Add the Ollama provider and bind its credential. A global key inherits nothing |
   | Key Budget | Unlimited budget |
   | Policies, rate limits, expiry | Leave empty for the lab |

   Before saving, the Model Access summary should read 1 model, 1 provider, 1 credential.
6. Copy the secret key when it appears. It is shown only once.

Inspection rules live under **Detectors**, **Guardrails** and **Policy**. Step 8 sets them up.

## Step 7: Point the agent at FortiAIGate

The agent talks to FortiAIGate with the OpenAI chat-completions API and never contacts Ollama directly. Four things change in the agent: the base URL, the model name, the API key and certificate trust.

- **Base URL:** `https://<faig-ip>/ai/openai/v1`. The gateway serves three dialect namespaces, `/ai/openai`, `/ai/anthropic` and `/ai/gemini`, and rejects any other path under `/ai`.
- **Model name:** FortiAIGate's public model name, not Ollama's. It is the provider slug plus the model ID with the colon normalized to a dash, so `deepseek-r1:7b` under slug `ollama` becomes `ollama-deepseek-r1-7b`. The `/models` call below lists the exact names a key may use.
- **API key:** the virtual key created in Step 6, not an Ollama value.
- **Certificate:** FortiAIGate serves a self-signed certificate. Copy `faig_deployment_assistant_data/deployment/web_certs/tls.crt` from the CLI's directory on the FortiAIGate server to the agent's machine and trust it. It is valid for the FortiAIGate server's IP addresses, `localhost` and `127.0.0.1`.
- **API style:** if the agent uses Ollama's native `/api/chat` or the `ollama` Python library, switch it to an OpenAI client first.

Test from the FortiAIGate server before changing the agent. Set the key and certificate path first, each as its own command.

```bash
read -rsp "Key: " FAIG_KEY; echo
CERT=$(find ~ -path '*deployment/web_certs/tls.crt' 2>/dev/null | head -1); echo "$CERT"
```

```bash
curl -sS --cacert "$CERT" https://<faig-ip>/ai/openai/v1/models \
  -H "Authorization: Bearer $FAIG_KEY"

curl -sS --max-time 180 -w '\nHTTP %{http_code}\n' --cacert "$CERT" \
  https://<faig-ip>/ai/openai/v1/chat/completions \
  -H "Authorization: Bearer $FAIG_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"ollama-qwen2.5","messages":[{"role":"user","content":"ping"}]}'
```

| Gateway error code | Meaning | Fix |
| --- | --- | --- |
| `no_such_namespace` | The path lacks a dialect prefix | Use `/ai/openai/v1/...` |
| `model_not_found` | The key cannot call that model name | Use a name from the `/models` list, or add the model to the key's Model Access |

Two ready-made clients use these settings. Keep both files in one directory on the machine that runs the agent, which is 10.20.70.5 in this lab.

| File | What it is |
| --- | --- |
| `faig_web.py` | Web console on port 8080. Shows each request's path, the gateway's decision, timing and tool calls |
| `faig_agent.py` | Command-line chat agent, and the core that the web console imports |

1. Copy both files to the agent machine and install the one dependency.

   ```bash
   sudo apt install -y python3-venv
   python3 -m venv ~/faig-agent
   ~/faig-agent/bin/pip install openai
   ```
2. Copy the gateway's certificate from the FortiAIGate server.

   ```bash
   scp <user>@<faig-ip>:<cli-directory>/faig_deployment_assistant_data/deployment/web_certs/tls.crt ~/faig-tls.crt
   ```
3. Start the web console, paste the virtual key when asked, and open `http://<agent-host>:8080`.

   ```bash
   FAIG_CA_CERT=~/faig-tls.crt ~/faig-agent/bin/python faig_web.py --host 0.0.0.0
   ```
4. Or start the command-line agent instead.

   ```bash
   FAIG_CA_CERT=~/faig-tls.crt ~/faig-agent/bin/python faig_agent.py
   ```

Both clients default to this lab's gateway URL and model; set `FAIG_BASE_URL` and `FAIG_MODEL` to change them. The virtual key stays in the server process and is never sent to the browser.

The web console has no login, so anyone who can reach port 8080 can send prompts with the key. Without `--host 0.0.0.0` it listens only on 127.0.0.1. Limit the port to your workstation with `sudo ufw allow from <workstation-ip> to any port 8080 proto tcp`. If you run the console on the Ollama server, the `ufw enable` from Step 6 is default-deny inbound and blocks 8080 until you add that rule; the console will report it is listening while the browser still cannot reach it.

The console runs until Ctrl+C and stops when the SSH session closes; start it inside `tmux` to keep it running.

Its starter prompts cover a one-word reply, a tool call and the canary string. Streaming and tools each have a switch, which isolates a failure to one of them.

## Step 8: Add a detector, a guardrail and a policy (FortiAIGate server WebUI)

Inspection is built from three objects: a detector holds the rules and the action, a guardrail bundles detectors, and a policy applies guardrails to models on the way in and on the way out. All three save without a GPU, but they enforce only when one is in use.

1. Open **Detectors**, click **Add Detector** and choose **Regex**. The other custom types are Data Leak Prevention and Natural Language.

   | Field | Value |
   | --- | --- |
   | Name | `lab-secrets-regex` |
   | Rule Name | `secrets-and-canary` |
   | Patterns | `FAIG-CANARY-7731` |
   |  | `-----BEGIN [A-Z ]*PRIVATE KEY-----` |
   |  | `AKIA[0-9A-Z]{16}` |
   | Action | Block |
2. Before saving, type `please repeat FAIG-CANARY-7731` into the **Playground** on that page and click **Run Test**. The Playground runs the deployment's own scanner and works without a GPU.
3. Open **Guardrails** and click **Create Guardrail**. Name it `lab-baseline`, add `lab-secrets-regex` from the Regex tab, add governance packs such as OWASP LLM Top 10 2025 Security Guard and General AI Safety Guard, and turn on **Deny Suspicious & invisible characters**.
4. Open **Policy** and click **Create Policy**.

   | Field | Value |
   | --- | --- |
   | Name | `lab-default` |
   | Models | Grant the Ollama provider |
   | Input guardrails | `lab-baseline` |
   | Output guardrails | `lab-baseline` |
   | Response scan mode | Post-stream, which scans the full response before delivery |
   | Apply to Organization | On |
5. Send the canary string through the gateway from the web console, then open **Logs** and read the Traffic Log. A verdict of Log means the request passed uninspected.

Fortinet ships 51 read-only governance packs, including OWASP LLM Top 10, OWASP Agentic Top 10, OWASP MCP Top 10, NIST AI RMF, MITRE ATLAS, IEC 62443 and NERC CIP.

In this lab the canary passed through with verdict Log, which confirms the WebUI banner: without a GPU, live traffic is not inspected, even by regex rules.

## Step 9: Set up the organization, workspaces, projects and users (FortiAIGate server WebUI)

The hierarchy runs organization, workspace, project, virtual key. Budgets and model access narrow at each level.

| Page | What it is for | State in this lab |
| --- | --- | --- |
| Organization | Deployment-wide name, external URL and default budgets | Name `Example Org`, External URL `http://www.example.org`, budgets unlimited |
| Workspaces | A team's keys, models and guardrails, with traffic and spend kept apart from other teams | None |
| Projects | Groups keys inside a workspace under a shared budget and model access | None |
| Users | Accounts and invitations, with spend against budget | One Super Admin |

1. On **Organization**, set a real name and set External URL to `https://<faig-ip>`. Invitation links and single sign-on URLs are built from that field.
2. On **Workspaces**, create a workspace for the lab, then create a project inside it on **Projects**.
3. On **Virtual Keys**, create a workspace-scoped key for the agent, restart the agent with it, and delete the global `lab-agent` key.

## Step 10: Add tools with an MCP server (agent machine)

The agent ships with three inline tools (time, calculator, subnet). To grow the tool set without editing the agent each time, it can act as an **MCP client**: it connects to one or more MCP servers, offers their tools to the model, and routes the model's tool calls to them. Tool execution happens agent-side, so the agent-to-MCP hop is not inspected, but the tool definitions, call arguments and results still travel through FortiAIGate in the conversation and are scanned there. An MCP server runs with the agent's privileges, so treat it as trusted code.

Three files in `agent/` provide this:

| File | What it is |
| --- | --- |
| `mcp_server.py` | A FastMCP server that re-implements the three inline tools over MCP. The starting point for adding new tools. |
| `mcp_bridge.py` | Runs the async MCP sessions in a background loop and exposes them to the synchronous agent. |
| `mcp.config.example.json` | A sample config in the standard `mcpServers` shape. |

MCP is off unless `FAIG_MCP_CONFIG` points at a config file, so the agent behaves exactly as before until you enable it.

1. Install the dependency (it is now in `requirements.txt`).

   ```bash
   ~/faig-agent/bin/pip install -r agent/requirements.txt   # adds mcp (<2)
   ```
2. Make a config from the example. A `command` of `python` runs under the agent's own interpreter, so the server shares the venv; `args` paths are relative to where you launch the agent (the repo root).

   ```bash
   cp agent/mcp.config.example.json agent/mcp.config.json
   ```
3. Launch the console (or `faig_agent.py`) with `FAIG_MCP_CONFIG` set.

   ```bash
   export FAIG_BASE_URL=https://<faig-ip>/ai/openai/v1
   export FAIG_MODEL=ollama-qwen2.5-7b
   export FAIG_MCP_CONFIG=agent/mcp.config.json
   FAIG_CA_CERT=~/faig-tls.crt ~/faig-agent/bin/python agent/faig_web.py --host 0.0.0.0
   ```

   On start it prints `MCP tools: calculate, get_current_time, subnet_info`. Because the MCP tool names match the inline ones, the MCP versions take over - the subnet starter still answers, now served by the MCP server, and the exchange appears in FortiAIGate's logs as before.

Use a tool-calling model such as `ollama-qwen2.5-7b`; a reasoning model like `deepseek-r1` calls tools unreliably. To add new tools, add `@mcp.tool()` functions to `mcp_server.py` (or point the config at another MCP server); no agent change is needed. Retrieval (RAG) plugs in the same way, as a `search` tool, in the next step.

## Step 11: Add retrieval (RAG) over documentation (agent machine)

RAG is added as one more MCP tool, `search_docs`, so it rides the same path as Step 10: when the model calls it, the agent embeds the query with Ollama, pulls the closest passages from a Qdrant vector store, and returns them. Those passages travel back through FortiAIGate as the tool's result, so **the gateway inspects what retrieval surfaces** - a doc chunk carrying a secret or PII is scanned before it reaches the model.

Four files in `agent/` provide this:

| File | What it is |
| --- | --- |
| `rag_common.py` | Document loading (PDF, Markdown, text, HTML), chunking, Ollama embeddings, and the Qdrant client. |
| `rag_ingest.py` | Builds or extends the index. Re-runnable: chunk ids are deterministic, so re-ingesting updates a file rather than duplicating it. |
| `rag_server.py` | A FastMCP server exposing the `search_docs` tool. |
| `mcp.config.rag.example.json` | A config that enables both the lab tools and the RAG server. |

All of this runs on the agent/Ollama host. Embeddings go straight to Ollama; only the search results pass through the gateway.

1. Run Qdrant (Docker) on the agent host. If Docker is not installed there, install it as in Step 3, item 1.

   ```bash
   docker run -d --name qdrant --restart unless-stopped \
     -p 6333:6333 -v ~/qdrant_storage:/qdrant/storage qdrant/qdrant
   ```
2. Pull an embedding model and install the Python dependencies.

   ```bash
   ollama pull nomic-embed-text
   ~/faig-agent/bin/pip install -r agent/requirements.txt   # adds qdrant-client, pypdf
   ```
3. Put documents where the ingester can read them, then build the index. The default folder is `rag_docs/` in the repo (git-ignored); or pass any file or folder.

   ```bash
   mkdir -p rag_docs
   # download the FortiSIEM 7.6 docs (PDF/HTML) into rag_docs/, then:
   ~/faig-agent/bin/python agent/rag_ingest.py
   # or a specific path:  ~/faig-agent/bin/python agent/rag_ingest.py ~/fortisiem-docs
   ```
4. Enable the RAG server in the agent's MCP config and restart the agent.

   ```bash
   cp agent/mcp.config.rag.example.json agent/mcp.config.json
   export FAIG_MCP_CONFIG=agent/mcp.config.json
   # relaunch faig_web.py as in Step 7
   ```

   On start it now prints `MCP tools: calculate, get_current_time, search_docs, subnet_info`. Ask a product question in the console; the model should call `search_docs`, and the retrieved passages appear in the reply and in FortiAIGate's logs.

To extend the index, drop more files in `rag_docs/` (or anywhere) and run `rag_ingest.py` again - no restart needed, since the server queries Qdrant live. Configuration (Ollama URL, Qdrant URL, collection name, chunk size, top-k) is in `rag_common.py` via `RAG_*` environment variables.

## Step 12: Performance testing (load generator + host monitor)

Two tools in `perf/` measure how FortiAIGate holds up under load. `faig_bench.py` drives concurrent queries and reports latency and throughput; `faig_monitor.py` samples CPU, memory and GPU on the FortiAIGate host while that runs. Because the LLM generation happens on Ollama and FortiAIGate's 3090 runs only the guardrail models, run the load **through the gateway** and **straight at Ollama** and compare - the difference is the gateway's inspection overhead, and the monitor shows where it lands.

1. On the **FortiAIGate server**, start the monitor (stdlib only; `docker stats` may need `sudo`).

   ```bash
   python3 perf/faig_monitor.py --interval 2 --out faig_load.csv
   ```

   It prints a live line (host CPU, each GPU, the busiest FAIG containers) and writes a tidy CSV: `timestamp,source,cpu_pct,mem_used_mb,mem_total_mb` with a row per host, `gpuN`, and `faig-*` container.
2. From the **agent side** (any host that can reach the gateway), drive load through FortiAIGate.

   ```bash
   FAIG_API_KEY=<key> FAIG_CA_CERT=~/faig-tls.crt \
     ~/faig-agent/bin/python perf/faig_bench.py \
     --base-url https://<faig-ip>/ai/openai/v1 --model ollama-qwen3.5-9b \
     --concurrency 10 --requests 200 --max-tokens 128 --warmup 5 --stream
   ```

   It reports success rate, throughput (req/s), latency percentiles (p50/p90/p95/p99), time-to-first-token (with `--stream`), aggregate output tokens/s, and an error breakdown (gateway refusals show as `HTTP 4xx/<code>`). `--csv run.csv` and `--json summary.json` save the raw data.
3. Repeat the same run **against Ollama directly** to isolate the gateway:

   ```bash
   ~/faig-agent/bin/python perf/faig_bench.py \
     --base-url http://<ollama-ip>:11434/v1 --model qwen3.5:9b \
     --concurrency 10 --requests 200 --max-tokens 128 --warmup 5 --stream
   ```

   The latency and throughput gap between the two runs is FortiAIGate's inspection cost; the monitor's CSV shows the matching CPU/GPU/memory the gateway spent.

Use `--duration <seconds>` instead of `--requests` to hold a fixed concurrency for a set time, and raise `--concurrency` in steps to find where latency climbs or errors appear. Keep `--max-tokens` fixed across runs so reply length does not skew the comparison.

## Verification and troubleshooting

The install is complete when every item below is ticked.

- [ ] `nvidia-smi` lists the expected card on each GPU host
- [ ] `./deploy-docker-compose.sh --self-test` ends with "Self-test passed"
- [ ] The preflight reports 0 failures
- [ ] Status and health shows every container up, `/ui` responding, and, in Options A and B, Triton and vLLM healthy
- [ ] The license is imported and the API has been restarted
- [ ] Ollama answers on `/v1/chat/completions` and the model sits on the intended card
- [ ] A container on the FortiAIGate server can reach port 11434; another machine cannot
- [ ] The agent gets a reply through FortiAIGate and the request appears in FortiAIGate's logs
- [ ] A prompt that a guard rule should block is blocked

| Symptom | Action |
| --- | --- |
| `nvidia-smi` cannot communicate with the driver after reboot | Secure Boot is blocking the module. On bare metal, enroll the MOK key at boot or disable Secure Boot. On a Proxmox VM, the `-open` module builds under DKMS but will not load with Secure Boot on: `dkms status` shows "installed", `sudo modprobe nvidia` returns "Key was rejected by service", and `mokutil --sb-state` reads enabled. Disable Secure Boot in the VM's OVMF, or recreate the EFI disk with `pre-enrolled-keys=0`. See Appendix A |
| Guest VM sees the card but passthrough misbehaves, or only some PCI functions bind to vfio | A Turing card (2080 Super) has four functions that must all reach the guest; the USB and UCSI functions need a boot-time rebind on the host. See Appendix A |
| First `APPLY` ended "dependency failed to start: faig-postgres is unhealthy" | PostgreSQL missed its first-start healthcheck under image-load contention and recovered on its own. Confirm `./compose.sh ps -a` shows postgres `healthy`, then re-run `./compose.sh up -d --remove-orphans` |
| License imported but functions stay locked / `/ai/` stays 403 | After `compose.sh restart api`, reboot the FortiAIGate host; on the 20 October build the license did not activate until a full reboot |
| Preflight: "NVIDIA runtime is incomplete" | Run `sudo nvidia-ctk runtime configure --runtime=docker`, then `sudo systemctl restart docker` |
| Preflight: port 443 answers, or Kubernetes detected | Something else holds the port. Remove it, or rerun the wizard with HTTPS port `9443` |
| Apply stops: vLLM GPU has compute capability 7.5 | Only the 2080 Super was selected. Rerun the wizard and select only the 3090 |
| "Target images are incomplete" or "missing archive" | An archive is absent or misnamed. Check the file names against Step 4 |
| Triton or vLLM never becomes healthy on the shared 3090 | Check their logs with **5**, then **3. Show recent logs**. vLLM reserves 80% of the card. If Triton runs out of GPU memory, lower `VLLM_GPU_MEMORY_UTILIZATION` in `.env`, regenerate and reapply |
| FortiAIGate cannot reach Ollama | Check `OLLAMA_HOST`, the firewall rule, and that the provider address is a LAN IP rather than `localhost` |
| Add Model: Test Connectivity fails with `context deadline exceeded` | The ~10 s probe timed out on a cold model load, not a broken path. Confirm a container can reach Ollama (`docker run --rm curlimages/curl -s http://<ollama-ip>:11434/v1/models`), pre-warm the model with one request, set `OLLAMA_KEEP_ALIVE`, then retry |
| A regex detector blocks every prompt, benign ones included | A pattern is matching all input — usually an empty pattern line (an empty regex matches everything) or an over-broad one such as `.*`. Open the detector, remove the blank/greedy pattern, and re-test in the Playground: a benign prompt should pass, the canary should block |
| Ollama loads the model on the 3090 in Option A | Check the `CUDA_VISIBLE_DEVICES` UUID in the override, restart Ollama, and recheck with `ollama ps` |
| A service is unhealthy | Main menu **5**, then **3. Show recent logs**, and enter the service name |
| Agent prints `MCP tools: none` or an `MCP server error` line | The MCP server did not start. Check the printed error; usually the `command`/`args` path in the config is wrong (args are relative to where you launch the agent) or `mcp` is not installed in the venv |
| MCP import error about `FastMCP` renamed to `MCPServer` | `mcp` 2.x is installed; this agent uses the v1 API. Reinstall with the pin: `~/faig-agent/bin/pip install "mcp>=1.2,<2"` |
| A tool never fires, model answers in prose instead | Use a tool-calling model (`ollama-qwen2.5-7b` or `ollama-qwen3.5-9b`), confirm **Offer tools** is on, and check the model actually fits in VRAM with `ollama ps` |
| `search_docs` returns `embedding failed` | Ollama is unreachable or the embedding model is missing. Check `RAG_OLLAMA_URL` and run `ollama pull nomic-embed-text` |
| `search_docs` returns `search failed` or `no matches` | Qdrant is not running (`docker ps` for the `qdrant` container) or the index is empty - run `rag_ingest.py` and confirm it reports chunks upserted |

## Open items

Routing, logging and the test agent work. Enforcement does not, and it cannot in this lab as built. Six points remain open.

- [ ] Get a GPU under FortiAIGate. Neither VM has one, so the policy cannot enforce. The options are a host with a supported NVIDIA card, a cloud instance with an A10 or L4, or asking the FortiAIGate product team whether an evaluation switch exists for CPU-only scanning.
- [ ] Do not try to emulate a GPU. The DLP and natural-language detectors need real CUDA hardware, and the GPU count comes from the license reporters in the engine containers.
- [ ] Replace the `lab-agent` test key with a fresh one; its value was shared during setup.
- [ ] Fix the Organization name and External URL, and create the workspace and project in Step 9.
- [ ] Confirm tool calls through the gateway with the web console's subnet prompt. A streamed reply has worked; tool calls are untested.
- [ ] If a single 24 GB card is used later, confirm that Triton and vLLM both load on it and that the host has the 64 GB the CLI recommends.

## Appendix A: GPU passthrough on Proxmox

When FortiAIGate and Ollama are VMs on one Proxmox host, each card is passed through to its VM before the Step 2 driver install. These are the points that cost time on the 20 October build.

**Host: bind both cards to vfio-pci before any guest driver can claim them.**

1. Enable **VT-d** and **Above 4G Decoding** in the host BIOS, and add `intel_iommu=on iommu=pt` to the kernel command line.
2. Bind every function of both cards to `vfio-pci` by vendor:device ID in `/etc/modprobe.d/vfio.conf`, and blacklist `nouveau` and `nvidia` on the host. The Ampere 3090 has only GPU + HD-audio and binds cleanly. A Turing card such as the 2080 Super exposes **four** functions — GPU, HD-audio, a USB 3.1 controller, and a USB-C UCSI controller — which all share one IOMMU group, so all four must reach the guest. The USB and UCSI functions get claimed by the host's `xhci_hcd` and `nvidia-gpu` drivers and will not move to vfio from `vfio.conf` alone; a small boot-time systemd unit that unbinds them and writes `vfio-pci` to each one's `driver_override`, then binds them, fixes it.
3. After a host reboot, confirm every function reads `Kernel driver in use: vfio-pci`:

   ```bash
   lspci -nnk -d 10de:
   ```

   Also confirm each card is alone in its IOMMU group. Clean isolation means no ACS override is needed.

**Guest: q35 + OVMF, GPU added after the OS, and Secure Boot off.**

- Create each GPU VM as machine type **q35** with **OVMF (UEFI)** firmware. SeaBIOS/i440fx is the fragile path for passthrough and a common source of code 43.
- Install Ubuntu **before** attaching the card, so the installer uses the virtual display. Then add the GPU and power-cycle:

  ```bash
  qm set <vmid> -hostpci0 <pci-address>,pcie=1     # e.g. 01:00 ; no x-vga=1 on Proxmox 8
  qm stop <vmid> && qm start <vmid>                # hostpci/vga changes need stop/start, not reboot
  ```

  Omitting `x-vga=1` keeps the noVNC console on the virtual display, so you do not lose the console when the card is attached.
- The NVIDIA `-open` driver builds under DKMS but will not load if the VM's OVMF has **Secure Boot enabled**: `nvidia-smi` reports "couldn't communicate with the driver", `dkms status` shows the module "installed", and `sudo modprobe nvidia` returns "Key was rejected by service". Disable Secure Boot in the VM's OVMF (Device Manager → Secure Boot Configuration), or recreate the EFI disk with `pre-enrolled-keys=0`, then load the module:

  ```bash
  mokutil --sb-state        # should read: SecureBoot disabled
  sudo modprobe nvidia
  nvidia-smi                # the card and ~24 GB (3090) or ~8 GB (2080 Super) appear
  ```

- If the noVNC console shows "unable to connect" after attaching the card, do a full `qm stop`/`qm start` and restart `pveproxy` on the host. Once the guest has an IP and SSH, drive the rest over SSH rather than the console.

With both guests showing their card in `nvidia-smi`, continue from Step 2 item 4 (record the UUIDs) and Step 3 (Docker and the container toolkit on the FortiAIGate host only).

## Sources

- FortiAIGate Docker Compose CLI user guide and `deploy-docker-compose.sh` 0.6.2, supplied with the 8.0.2 release
- [FortiAIGate 8.0.1 Administration Guide, Appendix B](https://docs.fortinet.com/document/fortiaigate/8.0.1/fortiaigate-administration-guide/92184/appendix-b-sample-configuration-for-ai-agents)
- [Triton Inference Server 25.11 release notes](https://docs.nvidia.com/deeplearning/triton-inference-server/archives/triton-inference-server-2670/release-notes/rel-25-11.html)
- [UbuntuHandbook: NVIDIA 580 driver in the official Ubuntu repositories](https://ubuntuhandbook.org/index.php/2025/09/ubuntu-added-nvidia-580-driver/)
- [Ollama library: qwen2.5](https://ollama.com/library/qwen2.5)
- [Ollama documentation: context length](https://docs.ollama.com/context-length)
