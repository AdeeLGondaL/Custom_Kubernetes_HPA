# Custom Kubernetes HPA — Elastic ML Inference Serving

A custom Horizontal Pod Autoscaler for ResNet18 image classification, compared against the built-in Kubernetes HPA at 70% and 90% CPU targets.

## Architecture

```
Load Tester ──► Dispatcher ──► ML Replica Pod(s)
                    │                   │
                    └─── /metrics ───────┘
                               │
                          Prometheus
                               │
                          Autoscaler ──► Kubernetes API
```

- **ML Replica** — FastAPI + ResNet18 (CPU-only). One request at a time per pod.
- **Dispatcher** — Centralized queue. Tracks live pod IPs via the K8s Watch API and routes requests to free replicas. Drops requests (HTTP 503) when queue is full.
- **Autoscaler** — Polls Prometheus every 15s and scales using: `desired = ceil(λ × W / target_utilization)` where λ is arrival rate and W is mean inference time.
- **Load Tester** — Async script that sends a configurable req/s workload to the dispatcher.

## Prerequisites

- Docker Desktop (with Minikube driver)
- kubectl
- Minikube
- Helm
- Python 3.11+

## Setup

### 1. Start Minikube

```bash
minikube start --cpus=6 --memory=8192
minikube addons enable metrics-server
```

### 2. Build images inside Minikube

```bash
# Linux/macOS
eval $(minikube docker-env)

# Windows PowerShell
& minikube -p minikube docker-env | Invoke-Expression
```

```bash
docker build -t ml-replica:latest ./ml_replica
docker build -t dispatcher:latest  ./dispatcher
docker build -t autoscaler:latest  ./autoscaler
```

### 3. Install Prometheus

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm install prometheus prometheus-community/kube-prometheus-stack \
  -f k8s/prometheus/values.yaml --namespace default
```

### 4. Deploy

```bash
kubectl apply -f k8s/autoscaler/serviceaccount.yaml
kubectl apply -f k8s/autoscaler/role.yaml
kubectl apply -f k8s/autoscaler/rolebinding.yaml
kubectl apply -f k8s/autoscaler/configmap.yaml
kubectl apply -f k8s/dispatcher/configmap.yaml
kubectl apply -f k8s/ml-replica/
kubectl apply -f k8s/dispatcher/
kubectl apply -f k8s/autoscaler/
```

Wait for all pods to be Running:
```bash
kubectl get pods -w
```

### 5. Get the dispatcher URL

```bash
# Linux/macOS
DISPATCHER_URL=$(minikube service dispatcher-service --url)/infer

# Windows PowerShell
$DISPATCHER_URL = "$(minikube service dispatcher-service --url)/infer"
```

Smoke test:
```bash
curl -X POST "$DISPATCHER_URL" -F "file=@load_tester/test_image.jpg"
# {"class": "golden retriever", "confidence": 0.84}
```

## Running the experiments

All three runs use the same workload (`workload.txt` — 630 seconds, peak 44 req/s).

### Run 1 — Custom autoscaler

```bash
kubectl delete hpa ml-replica-hpa --ignore-not-found
kubectl scale deployment autoscaler --replicas=1
kubectl scale deployment ml-replica --replicas=2

python load_tester/load_tester.py --url $DISPATCHER_URL --workload workload.txt --image load_tester/test_image.jpg
```

### Run 2 — Built-in HPA at 70% CPU

```bash
kubectl scale deployment autoscaler --replicas=0
kubectl scale deployment ml-replica --replicas=2
kubectl apply -f k8s/hpa/hpa-70.yaml
kubectl get hpa --watch   # wait until TARGETS shows a real %, not <unknown>

python load_tester/load_tester.py --url $DISPATCHER_URL --workload workload.txt --image load_tester/test_image.jpg
```

### Run 3 — Built-in HPA at 90% CPU

```bash
kubectl delete hpa ml-replica-hpa
kubectl scale deployment ml-replica --replicas=2
kubectl apply -f k8s/hpa/hpa-90.yaml
kubectl get hpa --watch

python load_tester/load_tester.py --url $DISPATCHER_URL --workload workload.txt --image load_tester/test_image.jpg
```

### Restore

```bash
kubectl delete hpa ml-replica-hpa --ignore-not-found
kubectl scale deployment autoscaler --replicas=1
kubectl scale deployment ml-replica --replicas=2
```

## Running tests

```bash
pip install -r requirements-dev.txt
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pytest
```

## Project structure

```
├── ml_replica/       ResNet18 inference server
├── dispatcher/       Request router and queue
├── autoscaler/       Custom HPA control loop
├── load_tester/      Load generation script
├── k8s/              Kubernetes manifests
├── tests/            Unit tests
├── results/          Experiment figures (PNG + PDF)
└── workload.txt      Traffic profile used in all 3 experiments
```

## Known limitations

- The built-in HPA at 70%/90% CPU did not scale during the experiment. The dispatcher queue limits replica CPU to ~76%, which falls within Kubernetes HPA's built-in ±10% tolerance window (threshold = 77%). This is a fundamental limitation of CPU-based autoscaling when backpressure masks true demand — which is exactly what the custom autoscaler avoids by using arrival rate metrics directly.
- First inference after pod startup takes ~800ms (model JIT warmup). Subsequent requests settle to ~75-100ms.
