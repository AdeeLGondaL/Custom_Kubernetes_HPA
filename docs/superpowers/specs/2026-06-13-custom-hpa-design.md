# Custom Kubernetes HPA — Design Spec

**Date:** 2026-06-13  
**Status:** Approved  
**Project:** Custom Horizontal Pod Autoscaler for ML Inference Serving (ResNet18 / Minikube)

---

## 1. Goal

Build and compare three autoscaling strategies for a ResNet18 image-classification serving system on Minikube:

1. **Custom autoscaler** — Little's Law controller (this project's main deliverable)
2. **Built-in Kubernetes HPA** at 70% CPU target
3. **Built-in Kubernetes HPA** at 90% CPU target

Each strategy is evaluated under the same load profile. The comparison metric is **p99 latency + CPU cores used** plotted as time series.

---

## 2. System Architecture

### Request flow

```
Load Tester → Dispatcher → ML Replica Pod(s)
                  │
                  └── /metrics ◄── Prometheus ◄── Autoscaler (PromQL queries)
                  
ML Replica Pod(s) └── /metrics ◄── Prometheus

Autoscaler ──────────────────────────────────► Kubernetes API Server (PATCH replicas)
```

### Components

| Component | Language | Runtime | Role |
|---|---|---|---|
| ML Replica | Python | FastAPI + uvicorn (1 worker) | Runs ResNet18 inference |
| Dispatcher | Python | FastAPI + asyncio | Smart router with queue and backpressure |
| Autoscaler | Python | asyncio loop | Little's Law controller |
| Load Tester | Python | script | Drives experiment traffic |
| Prometheus | — | Helm chart | Metrics storage and query |
| Grafana | — | Helm chart | Dashboard and results plots |

---

## 3. ML Replica

### Behaviour
- Loads ResNet18 (ImageNet weights) once at startup into a module-level variable
- Calls `model.eval()` and wraps inference in `torch.no_grad()`
- Handles exactly one request at a time (`--workers 1`)
- Accepts a POST `/infer` with a JPEG image, returns the top-1 class label and confidence

### Resources
```yaml
resources:
  requests: { cpu: "1", memory: "1Gi" }
  limits:   { cpu: "1", memory: "1Gi" }
```
Request == limit pins each replica to exactly 1 CPU core, making the experiment fair.

### Docker image
- Base: `python:3.11-slim`
- CPU-only PyTorch: `pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu`
- Built into Minikube's Docker daemon; `imagePullPolicy: Never`

### Metrics exposed (`/metrics`)
| Metric | Type |
|---|---|
| `inference_duration_seconds` | Histogram |
| `requests_total` | Counter |
| `requests_in_progress` | Gauge |

---

## 4. Dispatcher

### Behaviour
- Exposes POST `/infer` — the single entry point for all client traffic
- Maintains a live set of free replica Pod IPs via the **Kubernetes Watch API** (streaming `ADDED` / `MODIFIED` / `DELETED` events filtered to `app=ml-replica` Pods in `Running` state)
- Routes each incoming request to a free replica (pop from free set → forward → return to free set)
- If no replicas are free: request waits in an **asyncio bounded queue** (depth: `MAX_QUEUE_DEPTH`)
- If queue is full: returns **HTTP 503** immediately (backpressure / drop)
- Built with `asyncio` for I/O-bound concurrency on a single thread

### Configuration (environment variables via ConfigMap)
| Variable | Default | Meaning |
|---|---|---|
| `MAX_QUEUE_DEPTH` | 50 | Max requests waiting before 503 |

### Metrics exposed (`/metrics`)
| Metric | Type |
|---|---|
| `dispatcher_queue_depth` | Gauge |
| `dispatcher_dropped_requests_total` | Counter |
| `dispatcher_requests_total` | Counter |
| `dispatcher_request_duration_seconds` | Histogram |
| `dispatcher_active_replicas` | Gauge |

---

## 5. Autoscaler

### Control loop (15s interval)

```
observe  →  query Prometheus for λ and W
analyze  →  desired = ceil(λ × W / 0.7), clamped to [MIN, MAX]
act      →  if desired ≠ current AND cooldown elapsed: PATCH Deployment replicas
wait     →  sleep 15s
```

### Little's Law computation

```
λ (arrival rate)     = Δdispatcher_requests_total / Δt
W (mean service time) = Δinference_duration_seconds_sum / Δinference_duration_seconds_count
desired_replicas     = ceil(λ × W / TARGET_UTILIZATION)
```

### Configuration (environment variables via ConfigMap)
| Variable | Default | Meaning |
|---|---|---|
| `TARGET_UTILIZATION` | 0.7 | Headroom factor |
| `MIN_REPLICAS` | 1 | Floor — keep one warm replica |
| `MAX_REPLICAS` | 10 | Ceiling — protect the Minikube node |
| `SCALE_UP_COOLDOWN` | 30s | Minimum seconds between scale-up events |
| `SCALE_DOWN_COOLDOWN` | 180s | Minimum seconds between scale-down events |
| `POLL_INTERVAL` | 15s | Control loop sleep interval |
| `PROMETHEUS_URL` | `http://prometheus:9090` | Prometheus endpoint |

### Kubernetes access
- Runs with a dedicated `ServiceAccount`
- A `Role` grants `get` + `patch` on `deployments/scale`
- A `RoleBinding` links the Role to the ServiceAccount
- Uses `config.load_incluster_config()` — credentials auto-mounted by Kubernetes

---

## 6. Monitoring

### Prometheus scrape config (static)
```yaml
scrape_configs:
  - job_name: 'dispatcher'
    static_configs:
      - targets: ['dispatcher-service:9090']
  - job_name: 'ml-replicas'
    static_configs:
      - targets: ['ml-replica-service:9090']
```
Prometheus scrapes Kubernetes Service DNS names every 15s. Both components expose `/metrics` via `prometheus-client`.

### Installation
```bash
helm install prometheus prometheus-community/kube-prometheus-stack \
  --set prometheus.prometheusSpec.scrapeInterval=15s
```

### Grafana dashboard panels
| Panel | PromQL |
|---|---|
| Request rate | `rate(dispatcher_requests_total[1m])` |
| p99 latency | `histogram_quantile(0.99, rate(inference_duration_seconds_bucket[1m]))` |
| Replica count | `kube_deployment_spec_replicas{deployment="ml-replica"}` |
| Queue depth | `dispatcher_queue_depth` |
| Dropped requests | `rate(dispatcher_dropped_requests_total[1m])` |

---

## 7. Repo Structure

```
Custom_Kubernetes_HPA/
├── ml_replica/
│   ├── app.py
│   ├── Dockerfile
│   └── requirements.txt
├── dispatcher/
│   ├── app.py
│   ├── Dockerfile
│   └── requirements.txt
├── autoscaler/
│   ├── autoscaler.py
│   ├── Dockerfile
│   └── requirements.txt
├── load_tester/
│   ├── load_tester.py
│   └── requirements.txt
├── k8s/
│   ├── ml-replica/
│   │   ├── deployment.yaml
│   │   └── service.yaml
│   ├── dispatcher/
│   │   ├── deployment.yaml
│   │   └── service.yaml
│   ├── autoscaler/
│   │   ├── deployment.yaml
│   │   ├── serviceaccount.yaml
│   │   ├── role.yaml
│   │   └── rolebinding.yaml
│   └── hpa/
│       ├── hpa-70.yaml
│       └── hpa-90.yaml
└── docs/
    ├── concepts.md
    └── superpowers/specs/
        └── 2026-06-13-custom-hpa-design.md
```

---

## 8. Experiment Plan

Three runs under identical load profiles:

| Run | Autoscaler | Scale signal |
|---|---|---|
| 1 | Custom (this project) | Little's Law on arrival rate + service time |
| 2 | Built-in HPA | CPU utilization ≥ 70% |
| 3 | Built-in HPA | CPU utilization ≥ 90% |

**Comparison plots** (time series, same x-axis for all three runs):
- p99 inference latency
- Number of active replicas (CPU cores used)

Between runs: reset the Deployment to 1 replica, delete/re-apply the appropriate autoscaler manifest, wait for steady state, then start the load tester.

---

## 9. Simplifications vs Production

| Area | What we do | What production does |
|---|---|---|
| Prometheus target discovery | Static scrape config | Kubernetes service discovery (auto-discovers Pods by label) |
| Replica metrics scraping | Via Service (aggregate) | Per-Pod scraping with pod-name labels |
| RBAC management | Single static YAML manifest | Helm/OPA for audit-trail management at scale |
| Image registry | Minikube local daemon + `imagePullPolicy: Never` | Private registry (ECR, GCR, etc.) with CI/CD push |
| ML model | ResNet18, CPU-only | GPU-accelerated, batched inference, model serving frameworks (Triton, TorchServe) |
