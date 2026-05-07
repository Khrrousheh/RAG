# Compute Entity Specification

This document defines the requested test service compute entity for deploying
the Policy RAG Chatbot on AWS EC2.

## Purpose

Provision a test/UAT environment for a company-policy RAG chatbot.

The service runs:

- React/Vite frontend
- FastAPI backend
- Qdrant vector database
- Sentence Transformers embedding model
- local LLM runtime through Docker Model Runner or an Ollama-compatible endpoint

## Recommended Test Environment

| Item | Specification |
| --- | --- |
| Cloud provider | AWS |
| Service | EC2 |
| Environment | Test / UAT |
| Operating system | Windows Server 2022 or Windows Server 2025 |
| Recommended instance | `g6.2xlarge` |
| vCPU | 8 |
| RAM | 32 GiB |
| GPU | 1 x NVIDIA L4 |
| GPU memory | 24 GB class |
| Root volume | 250 GiB EBS gp3 |
| Baseline storage performance | 3,000 IOPS, 125 MiB/s |
| Public access | HTTPS only |
| Admin access | RDP restricted to approved company IPs |

## Minimum Functional Test Environment

Use this only for functional testing without realistic LLM latency expectations.

| Item | Specification |
| --- | --- |
| Instance | `m7i.2xlarge` or equivalent |
| vCPU | 8 |
| RAM | 32 GiB |
| GPU | None |
| Root volume | 200 GiB EBS gp3 |

Expected limitation: CPU-only LLM generation can be very slow. This is acceptable
for API/UI validation, but not for realistic chatbot response-time testing.

## Preferred GPU Options

| Option | Instance | vCPU | RAM | GPU | Best For |
| --- | --- | ---: | ---: | --- | --- |
| Recommended | `g6.2xlarge` | 8 | 32 GiB | 1 x NVIDIA L4 | Test service with local LLM inference |
| More headroom | `g6.4xlarge` | 16 | 64 GiB | 1 x NVIDIA L4 | Heavier prompts, more users, safer test margin |
| Alternative | `g5.2xlarge` | 8 | 32 GiB | 1 x NVIDIA A10G | Good ML inference alternative if G6 is unavailable |

## Storage Layout

Recommended single-volume test layout:

| Path / Use | Size Guidance |
| --- | ---: |
| OS and tools | 60-80 GiB |
| Docker/container data or service runtime | 60-100 GiB |
| Qdrant data | 20-100 GiB, depending on corpus size |
| model/cache data | 40-100 GiB |
| logs and temporary files | 20-40 GiB |

Recommended EBS settings:

- Volume type: `gp3`
- Size: 250 GiB for GPU test environment
- Encryption: enabled
- IOPS: 3,000 minimum
- Throughput: 125 MiB/s minimum

## Required Network Ports

Public exposure should be minimal.

| Port | Service | Exposure |
| ---: | --- | --- |
| 443 | HTTPS reverse proxy | Public or company network |
| 80 | HTTP redirect to HTTPS | Optional public |
| 3389 | RDP | Approved admin IPs only |
| 8000 | FastAPI backend | Internal only |
| 5173 | Vite/frontend dev server | Internal only or behind proxy |
| 6333 / 6334 | Qdrant | Internal only |
| 12434 | Docker Model Runner / Ollama-compatible endpoint | Internal only |

## Security Requirements

- Encrypt the EBS volume.
- Do not expose Qdrant publicly.
- Do not expose the backend API directly unless protected by HTTPS and network controls.
- Do not expose the LLM runtime publicly.
- Restrict RDP to approved IP addresses.
- Store secrets in environment variables or the company-approved secret manager.
- Treat policy PDFs, Qdrant data, prompts, generated answers, and benchmark logs as confidential.
- Enable OS patching and monitoring according to company policy.

## Software Requirements

| Software | Version / Notes |
| --- | --- |
| Git | Current stable |
| Python | 3.12 |
| Node.js | 22 |
| Docker | See Windows deployment note below |
| NVIDIA driver | Required for GPU instances |
| Reverse proxy | IIS, Nginx, Caddy, or company standard |
| Monitoring | CloudWatch agent or company standard |

## Important Windows Container Note

Docker Desktop is not supported on Windows Server. The current repository uses
Linux container images, including:

- `python:3.12-slim`
- `node:22-alpine`
- `qdrant/qdrant`

For a Windows Server test VM, support must choose one of these deployment models:

1. Run Linux containers through a supported Linux VM/WSL2/container host path.
2. Deploy services natively on Windows without Docker Compose.
3. Use a Linux EC2 instance instead of Windows Server for the containerized stack.

For the smoothest container deployment, Ubuntu Server on EC2 is the preferred
runtime. For a Windows-based test entity, the support team should confirm the
approved Linux-container strategy before provisioning.

## Acceptance Checklist

- EC2 instance is provisioned with the agreed instance type.
- EBS gp3 volume is encrypted and sized at 200-250 GiB or higher.
- RDP is restricted to approved IPs.
- HTTPS endpoint is available or reverse proxy plan is approved.
- Internal ports are not publicly exposed.
- GPU drivers are installed and `nvidia-smi` works, if GPU instance is used.
- Docker/container strategy is approved for Windows Server.
- Python 3.12 and Node.js 22 are available.
- Application repo can be cloned.
- Test policy PDFs can be placed securely on the machine.

## References

- Docker Desktop Windows Server support note: https://docs.docker.com/desktop/setup/install/windows-install/
- Microsoft Windows container host setup: https://learn.microsoft.com/en-us/virtualization/windowscontainers/quick-start/set-up-environment
- AWS EC2 G6 instances: https://aws.amazon.com/ec2/instance-types/g6/
- AWS EBS gp3 volumes: https://docs.aws.amazon.com/ebs/latest/userguide/general-purpose.html
