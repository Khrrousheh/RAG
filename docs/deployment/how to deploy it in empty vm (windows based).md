# How To Deploy On An Empty Windows-Based VM

This guide is written for a clean Windows Server test VM.

Important: Docker Desktop is not supported on Windows Server. The application
currently uses Linux container images, so support must either provide an
approved Linux-container runtime on the Windows host, run a Linux VM/container
host beside it, or deploy the services natively on Windows.

The steps below start with Docker because the application is currently packaged
with Docker Compose.

## 1. Prepare The VM

Log in with an administrator account using RDP.

Open PowerShell as Administrator and update the server:

```powershell
sconfig
```

Install all required Windows updates, then reboot if needed.

Confirm PowerShell and OS information:

```powershell
$PSVersionTable
Get-ComputerInfo | Select-Object WindowsProductName, WindowsVersion, OsHardwareAbstractionLayer
```

## 2. Install Docker Runtime

### Option A: Company-Approved Linux Container Runtime

Use this option if your support team has an approved way to run Linux containers
on Windows Server, such as a managed Linux VM, WSL2-based internal standard, or
container platform.

The deployed runtime must support:

- Linux containers
- Docker Compose
- bind mounts or named volumes
- outbound access to model and package registries
- GPU access if using a GPU instance

After installation, verify:

```powershell
docker version
docker compose version
docker run --rm hello-world
```

If GPU is required, verify the GPU is visible from containers according to the
runtime selected by support.

### Option B: Windows Server Docker Engine

Microsoft provides a Docker Engine setup path for Windows Server containers.
However, this repository's current Dockerfiles are Linux-based. Installing
Windows Server Docker Engine alone is not enough to run this Compose stack.

Install Docker Engine only if support plans to convert the app to Windows
containers or run Windows-native service containers.

PowerShell:

```powershell
Invoke-WebRequest `
  -UseBasicParsing `
  "https://raw.githubusercontent.com/microsoft/Windows-Containers/Main/helpful_tools/Install-DockerCE/install-docker-ce.ps1" `
  -OutFile install-docker-ce.ps1

.\install-docker-ce.ps1
Restart-Computer
```

After reboot:

```powershell
docker version
docker info
```

Stop here and confirm the Linux-container strategy before trying to run this
repository's Compose file.

## 3. Install Base Tools

Install Git:

```powershell
winget install --id Git.Git -e
```

Install Python 3.12:

```powershell
winget install --id Python.Python.3.12 -e
```

Install Node.js 22:

```powershell
winget install --id OpenJS.NodeJS.LTS -e
```

Restart PowerShell, then verify:

```powershell
git --version
python --version
node --version
npm --version
```

## 4. Install NVIDIA Driver For GPU Instances

Skip this section for CPU-only test instances.

Install the AWS/NVIDIA driver approved by your company for the selected GPU
instance type.

After installation and reboot:

```powershell
nvidia-smi
```

Expected result: the command lists the NVIDIA GPU, driver version, and memory.

## 5. Clone The Repository

Choose a deployment folder:

```powershell
New-Item -ItemType Directory -Force C:\apps
Set-Location C:\apps
git clone <REPOSITORY_URL> RAG
Set-Location C:\apps\RAG
```

Replace `<REPOSITORY_URL>` with the company repository URL.

## 6. Create Environment File

If `.env.example` exists:

```powershell
Copy-Item .env.example .env
notepad .env
```

Recommended Windows test values:

```env
QDRANT_HOST_PORT=6334
QDRANT_COLLECTION=company_policies_structural
EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
OLLAMA_MODEL=ai/gemma3-qat
OLLAMA_NUM_PREDICT=384
OLLAMA_KEEP_ALIVE=30m
PROMPT_CONTEXT_MAX_CHARS=3600
PROMPT_MIN_SOURCES=3
PROMPT_MAX_SOURCES=5
EMBEDDING_CACHE_SIZE=256
```

Do not commit `.env`.

## 7. Place Policy PDFs

Create the local policy folder if it does not exist:

```powershell
New-Item -ItemType Directory -Force policies
```

Copy the approved test policy PDFs into:

```text
C:\apps\RAG\policies
```

These files are confidential local runtime data.

## 8. Start Qdrant

If Linux containers are available and Compose is approved:

```powershell
docker compose up -d qdrant
docker compose ps
```

The host Qdrant port defaults to `6334`.

Verify Qdrant:

```powershell
Invoke-RestMethod http://localhost:6334/collections
```

## 9. Install Host Python Dependencies For Ingestion

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## 10. Ingest Policies

Run ingestion against the host-mapped Qdrant port:

```powershell
python -m EDA.structural_policy_ingest `
  --qdrant-url http://localhost:6334 `
  --recreate
```

Successful ingestion should report discovered PDFs, ingested PDFs, skipped PDFs,
total chunks, and the Qdrant collection name.

## 11. Start The Full Application

If Linux containers are available:

```powershell
docker compose up --build -d
docker compose ps
```

Open:

```text
http://localhost:5173
http://localhost:8000/docs
```

Check API health:

```powershell
Invoke-RestMethod http://localhost:8000/health
```

## 12. Run A Smoke Test

Search:

```powershell
$body = @{
  query = "Can I share progress about this project on LinkedIn?"
  top_k = 6
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri http://localhost:8000/search `
  -Method POST `
  -ContentType "application/json" `
  -Body $body
```

Streaming chat:

```powershell
$body = @{
  message = "Can I share progress about this project on LinkedIn?"
  top_k = 6
} | ConvertTo-Json

Invoke-WebRequest `
  -Uri http://localhost:8000/chat/stream `
  -Method POST `
  -ContentType "application/json" `
  -Body $body
```

## 13. Optional Benchmark

```powershell
.\.venv\Scripts\Activate.ps1
python benchmarks\p0_latency_benchmark.py `
  --api-base http://localhost:8000 `
  --llm-base http://localhost:12434 `
  --model ai/gemma3-qat `
  --samples 2 `
  --timeout 240
```

Save benchmark output according to company policy. Do not share benchmark output
if it contains sensitive policy text or prompts.

## 14. Production-Like Reverse Proxy

For a test service, expose only HTTPS to users. Keep backend, Qdrant, and model
ports internal.

Recommended public ports:

| Port | Purpose |
| ---: | --- |
| 443 | HTTPS |
| 80 | Optional HTTP to HTTPS redirect |
| 3389 | RDP from approved admin IPs only |

Do not publicly expose:

- `8000`
- `5173`
- `6333`
- `6334`
- `12434`

## 15. Native Windows Fallback Plan

If support cannot run Linux containers on Windows Server, deploy natively:

1. Run Qdrant on a separate Linux EC2 instance or approved container platform.
2. Run the FastAPI backend as a Windows service with Python 3.12.
3. Build the frontend with `npm run build`.
4. Serve `frontend/dist` through IIS, Caddy, or Nginx.
5. Run the LLM on an approved local or remote Ollama-compatible endpoint.
6. Set backend environment variables to point to Qdrant and the LLM endpoint.

This path requires additional service-management work but avoids unsupported
Docker Desktop usage on Windows Server.

## 16. Troubleshooting

### Docker Desktop install fails

Expected on Windows Server. Docker Desktop is not supported on Windows Server.
Use the company-approved Linux-container runtime or native deployment plan.

### Qdrant is unreachable from host

Check:

```powershell
docker compose ps
Invoke-RestMethod http://localhost:6334/collections
```

### Backend health reports Qdrant error

Inside Docker, backend expects:

```text
QDRANT_URL=http://qdrant:6333
```

From the Windows host, ingestion expects:

```text
http://localhost:6334
```

### Model is missing

Pull or install the model according to the selected LLM runtime:

```powershell
docker model pull ai/gemma3-qat
```

or use the company-approved Ollama/model-runtime command.

### GPU is not detected

Verify:

```powershell
nvidia-smi
```

Then verify the selected container/runtime path supports GPU passthrough.

## References

- Docker Desktop Windows Server support note: https://docs.docker.com/desktop/setup/install/windows-install/
- Microsoft Windows container setup: https://learn.microsoft.com/en-us/virtualization/windowscontainers/quick-start/set-up-environment
- Microsoft Docker Engine configuration on Windows: https://learn.microsoft.com/en-us/virtualization/windowscontainers/manage-docker/configure-docker-daemon
- AWS NVIDIA driver guidance: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/install-nvidia-driver.html
