# FortiAIGate
This repository is designed to help deploy, configure, and operationalize a FortiAIGate specifically version 8.0.2.  This is based on build 48 binaries.  Download those binaries from your Fortinet SE.
The FortiAIGate 8.0.2 + Ollama Lab instructions book will create an environment suitable for testing.  

Requirements
Sufficient storage (min of 250G is recommended for upgrades and logging.  
If you have a GPU (required for this to ultimately work). RAM needs to be 64G.  If you are using with out a GPU (only routing will work) 32G is required.
There are 3 deployment options.  1 All in one with a GPU.  2 2 linux servers one with the FortiAIGate and the other with an Ollama LLM both with a GPU.  I have a 3090 and a 2080 super so I'm using those as examples.  3 2 seperate ubuntu 22.04 deployed linux systems running both on proxmox.
1 network card.
Access to the internet.
