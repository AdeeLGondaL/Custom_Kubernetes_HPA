# Custom Kubernetes HPA — ML Inference Autoscaler

A custom Horizontal Pod Autoscaler (HPA) for ResNet18 image-classification inference, deployed on Minikube. The system auto-scales inference replicas using **Little's Law** and is compared against the built-in Kubernetes HPA at two CPU thresholds (70% and 90%).

## What this project does

```
Load Tester ──► Dispatcher ──► ML Replica Pod(s)
                    │
                    └── /metrics ◄── Prometheus ◄── Autoscaler
                                                         │
                                          Kubernetes API (PATCH replicas)
```

- **ML Replica** — FastAPI server that runs ResNet18 image classification (CPU-only PyTorch)
- **Dispatcher** — Smart router that tracks live Pod IPs via the Kubernetes Watch API and applies backpressure (HTTP 503) when all replicas are busy
- **Autoscaler** — Control loop that queries Prometheus every 15 s and scales replicas using: `desired = ceil(arrival_rate × mean_service_time / 0.7)`
- **Load Tester** — Async script that drives configurable requests-per-second traffic against the dispatcher

---

## Prerequisites

You need four tools installed before you can deploy anything. Tests and local development only need Python.

### Linux (Ubuntu/Debian)

**Docker**
```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io
sudo usermod -aG docker $USER && newgrp docker
```

**kubectl**
```bash
curl -LO "https://dl.k8s.io/release/$(curl -sL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
chmod +x kubectl && sudo mv kubectl /usr/local/bin/
```

**Minikube**
```bash
curl -LO https://storage.googleapis.com/minikube/releases/latest/minikube-linux-amd64
chmod +x minikube-linux-amd64 && sudo mv minikube-linux-amd64 /usr/local/bin/minikube
```

**Helm**
```bash
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
```

### Windows

Open PowerShell as Administrator.

**Option A — Chocolatey (easiest)**
```powershell
# Install Chocolatey first if you don't have it
Set-ExecutionPolicy Bypass -Scope Process -Force
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))

# Then install all tools at once
choco install docker-desktop kubernetes-cli minikube kubernetes-helm -y
```

Launch **Docker Desktop** from the Start Menu and wait for "Engine running" before continuing.

**Option B — Manual installers**

| Tool | Where to get it |
|---|---|
| Docker Desktop | https://www.docker.com/products/docker-desktop/ — run the `.exe`, enable WSL 2 backend |
| kubectl | https://dl.k8s.io/release/v1.30.0/bin/windows/amd64/kubectl.exe — place on your PATH |
| Minikube | https://storage.googleapis.com/minikube/releases/latest/minikube-installer.exe |
| Helm | https://get.helm.sh/helm-v3.15.0-windows-amd64.zip — extract `helm.exe` to your PATH |

**Verify everything works** (new terminal after installation):
```
docker version
kubectl version --client
minikube version
helm version
```

---

## Running the tests (no Kubernetes needed)

The unit tests cover all three components and run entirely locally.

```bash
# Clone the repo
git clone <repo-url>
cd Custom_Kubernetes_HPA

# Install dev dependencies
pip install -r requirements-dev.txt
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

# Run all tests
pytest

# Or run one component at a time
pytest tests/ml_replica/ -v
pytest tests/dispatcher/ -v
pytest tests/autoscaler/ -v
```

Expected output: **19 passed**.

---

## Running components locally (no Kubernetes)

Useful for manual testing and development.

**ML Replica** (starts on port 8080)

Linux/macOS:
```bash
cd ml_replica
pip install -r requirements.txt
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
uvicorn app:app --host 0.0.0.0 --port 8080 --workers 1
```

Test it:

Linux/macOS:
```bash
curl -X POST http://localhost:8080/infer -F "file=@load_tester/test_image.jpg"
# {"class": "...", "confidence": 0.84}
```

Windows (PowerShell):
```powershell
curl.exe -X POST http://localhost:8080/infer -F "file=@load_tester/test_image.jpg"
# {"class": "...", "confidence": 0.84}
```

> Note: In PowerShell, `curl` is an alias for `Invoke-WebRequest` (different syntax). Use `curl.exe` to get the real curl binary.

**Dispatcher** (starts on port 8081, K8s watch disabled)

Linux/macOS:
```bash
cd dispatcher
pip install -r requirements.txt
DISABLE_K8S_WATCH=1 uvicorn app:app --host 0.0.0.0 --port 8081 --workers 1
```

Windows (PowerShell):
```powershell
cd dispatcher
pip install -r requirements.txt
$env:DISABLE_K8S_WATCH="1"
uvicorn app:app --host 0.0.0.0 --port 8081 --workers 1
```

> The autoscaler requires a live Kubernetes cluster and Prometheus — it cannot run outside of Minikube.

---

## Full deployment on Minikube

### Step 1 — Start the cluster

```bash
minikube start --cpus=6 --memory=8192 --disk-size=30g
minikube addons enable metrics-server
```

`--cpus=6 --memory=8192` reserves 6 CPU cores and 8 GB RAM for Minikube. The experiment needs enough headroom to scale up to ~4 replicas (each pinned to 1 CPU).

### Step 2 — Build images inside Minikube's Docker daemon

Minikube runs its own Docker daemon. Pointing your shell at it lets you build images that Minikube can use directly — no registry needed.

> **Important:** This step only affects `docker build` commands. You only need to run it in the terminal where you're building images. All `kubectl` commands talk directly to the Kubernetes API and work in any terminal without this.

Linux/macOS:
```bash
eval $(minikube docker-env)
```

Windows (PowerShell):
```powershell
& minikube -p minikube docker-env | Invoke-Expression
```

Verify it worked:

Linux/macOS:
```bash
docker info | grep Name   # should show: Name: minikube
```

Windows (PowerShell):
```powershell
docker info | Select-String "Name"   # should show: Name: minikube
```

Now build all three images in that same terminal:
```bash
docker build -t ml-replica:latest ./ml_replica
docker build -t dispatcher:latest  ./dispatcher
docker build -t autoscaler:latest  ./autoscaler
```

> The ML Replica image takes ~5 minutes the first time (downloads the CPU PyTorch wheel, ~700 MB). Subsequent builds are fast due to layer caching.

### Step 3 — Apply RBAC and config

These `kubectl` commands can be run in any terminal (no Docker env setup needed):

```bash
kubectl apply -f k8s/autoscaler/serviceaccount.yaml
kubectl apply -f k8s/autoscaler/role.yaml
kubectl apply -f k8s/autoscaler/rolebinding.yaml
kubectl apply -f k8s/autoscaler/configmap.yaml
kubectl apply -f k8s/dispatcher/configmap.yaml
```

### Step 4 — Deploy the application

```bash
kubectl apply -f k8s/ml-replica/
kubectl apply -f k8s/dispatcher/
kubectl apply -f k8s/autoscaler/
```

### Step 5 — Wait for everything to come up

```bash
kubectl get pods -w
```

> This can be run in any terminal — no Docker env needed. Press Ctrl+C once all pods show Running.

Expected final state (takes ~60 s — the ML Replica loads ResNet18 weights at startup):
```
NAME                          READY   STATUS    RESTARTS
autoscaler-xxx                1/1     Running   0
dispatcher-xxx                1/1     Running   0
ml-replica-xxx                1/1     Running   0
```

### Step 6 — Expose the dispatcher and smoke test

On Windows, Minikube runs inside Docker and its network is not directly reachable from your machine. `minikube service --url` creates a temporary tunnel — but the URL only works while that process is running (it dies on Ctrl+C). Use `kubectl port-forward` instead, which gives you a stable `localhost` URL that you control.

**Open a dedicated terminal and keep it running for the rest of the session:**

```bash
kubectl port-forward svc/dispatcher-service 8080:8080
```

Leave that terminal open. Your dispatcher is now reachable at `http://localhost:8080`.

**In a separate terminal**, run the smoke test:

Linux/macOS:
```bash
curl -X POST http://localhost:8080/infer -F "file=@load_tester/test_image.jpg"
# {"class": "golden retriever", "confidence": 0.84}
```

Windows (PowerShell):
```powershell
curl.exe -X POST http://localhost:8080/infer -F "file=@load_tester/test_image.jpg"
# {"class": "golden retriever", "confidence": 0.84}
```

---

## Monitoring — Prometheus and Grafana

### Install

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm install prometheus prometheus-community/kube-prometheus-stack \
  -f k8s/prometheus/values.yaml \
  --namespace default
```

Wait for Prometheus pods to be ready:
```bash
kubectl get pods -l "release=prometheus" -w
```

### Verify scraping

Open a dedicated terminal and keep it running:
```bash
kubectl port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090
```

Open `http://localhost:9090/targets` in a browser — both `dispatcher` and `ml-replicas` should show **State: UP**.

### Open Grafana

Open another dedicated terminal and keep it running:
```bash
kubectl port-forward svc/prometheus-grafana 3000:80
```

Open `http://localhost:3000` — login: `admin` / `admin`.

Create a dashboard with these panels:

| Panel | PromQL |
|---|---|
| Request Rate | `rate(dispatcher_requests_total[1m])` |
| p99 Inference Latency | `histogram_quantile(0.99, rate(inference_duration_seconds_bucket[1m]))` |
| Replica Count | `kube_deployment_spec_replicas{deployment="ml-replica"}` |
| Queue Depth | `dispatcher_queue_depth` |
| Dropped Requests/s | `rate(dispatcher_dropped_requests_total[1m])` |

---

## Running the experiment

All three runs use the same load profile from `workload.txt` — a space-separated list of req/s values, one per second. The profile ramps up from a baseline (~7 req/s), hits a sustained peak (~44 req/s), then ramps back down. Each run takes as many seconds as there are values in the file.

The comparison metric is **p99 latency + replica count** over time.

### Load tester modes

```bash
# Workload-file mode (recommended for the experiment — uses workload.txt)
python load_tester.py --url http://localhost:8080/infer --workload ../workload.txt --image test_image.jpg

# Fixed-rate mode (useful for quick smoke tests)
python load_tester.py --url http://localhost:8080/infer --rate 20 --duration 60 --image test_image.jpg
```

In workload-file mode, the tester prints a progress line every 30 seconds so you can see it is running:
```
Workload: 600 seconds  peak=44 req/s  avg=18.3 req/s  total≈10980 requests
  t=30s  rate=8 req/s  queued=183
  t=60s  rate=32 req/s  queued=601
  ...
```

### Setup

Make sure the dispatcher port-forward from Step 6 is still running in its terminal. The dispatcher URL is `http://localhost:8080/infer` for all platforms.

### Run 1 — Custom autoscaler

```bash
# Confirm HPA is not running, custom autoscaler is
kubectl delete hpa ml-replica-hpa --ignore-not-found
kubectl get pods -l app=autoscaler   # should show Running

cd load_tester
python load_tester.py --url http://localhost:8080/infer --workload ../workload.txt --image test_image.jpg
```

Export the Grafana time-series screenshot. Label it **Run 1 — Custom**.

### Reset between runs

```bash
kubectl scale deployment ml-replica --replicas=1
# Wait 2 minutes before next run
```

### Run 2 — Built-in HPA at 70% CPU

```bash
kubectl scale deployment autoscaler --replicas=0
kubectl apply -f k8s/hpa/hpa-70.yaml
kubectl get hpa -w   # wait ~30s for HPA to initialise

python load_tester.py --url http://localhost:8080/infer --workload ../workload.txt --image test_image.jpg
```

Export screenshot. Label it **Run 2 — HPA 70%**.

Reset (same command as above).

### Run 3 — Built-in HPA at 90% CPU

```bash
kubectl delete hpa ml-replica-hpa
kubectl scale deployment ml-replica --replicas=1
# Wait 2 minutes

kubectl apply -f k8s/hpa/hpa-90.yaml
kubectl get hpa -w

python load_tester.py --url http://localhost:8080/infer --workload ../workload.txt --image test_image.jpg
```

Export screenshot. Label it **Run 3 — HPA 90%**.

### Restore cluster

```bash
kubectl delete hpa ml-replica-hpa --ignore-not-found
kubectl scale deployment autoscaler --replicas=1
kubectl scale deployment ml-replica --replicas=1
```

---

## Project structure

```
Custom_Kubernetes_HPA/
├── ml_replica/          FastAPI + ResNet18 inference server
├── dispatcher/          Async router with K8s watch and backpressure
├── autoscaler/          Little's Law control loop
├── load_tester/         Async load driver
├── k8s/
│   ├── ml-replica/      Deployment + Service
│   ├── dispatcher/      Deployment + Service + ConfigMap
│   ├── autoscaler/      Deployment + ConfigMap + RBAC
│   ├── hpa/             Built-in HPA manifests (70% and 90%)
│   └── prometheus/      Helm values for kube-prometheus-stack
├── tests/               Unit tests (pytest)
├── docs/
│   ├── concepts.md      All Kubernetes/distributed systems concepts explained
│   └── superpowers/
│       ├── specs/       Design specification
│       └── plans/       Implementation plan with step-by-step instructions
├── requirements-dev.txt Test dependencies
└── pytest.ini
```

---

## Useful commands

```bash
# Watch autoscaler decisions in real time
kubectl logs -l app=autoscaler -f

# Check what the dispatcher sees (replica count, queue depth)
kubectl logs -l app=dispatcher -f

# Scale manually for testing
kubectl scale deployment ml-replica --replicas=3

# Restart a component after rebuilding its image
kubectl rollout restart deployment/ml-replica

# Tear down everything
kubectl delete -f k8s/ml-replica/ -f k8s/dispatcher/ -f k8s/autoscaler/
helm uninstall prometheus
minikube stop
```

---

## Learn more

`docs/concepts.md` explains every concept used in this project — Kubernetes Pods/Deployments/Services, the Watch API, asyncio, Little's Law, Prometheus metrics, RBAC, and more — with notes on where simpler choices were made for this project vs. what production systems do.
