# Concepts & Learnings

A running log of every concept introduced during this project. Updated as we build.

---

## Kubernetes Fundamentals

### What Kubernetes Is
Kubernetes is a system that runs containers across a cluster of machines. You describe *what you want* in YAML files (e.g. "run 3 copies of this container"), and Kubernetes makes it happen and keeps it that way. You never say "start this container on machine X" — you declare desired state, and Kubernetes reconciles reality to match it. This pattern is called **declarative infrastructure**.

### Core Building Blocks

| Concept | What it is |
|---|---|
| **Pod** | The smallest deployable unit. One running container instance (or a tightly coupled group). Pods are ephemeral — they can be killed and replaced at any time. |
| **Deployment** | Manages N identical Pods. If a Pod crashes, the Deployment restarts it. You change `spec.replicas` to scale up or down. |
| **Service** | Gives a stable network address (DNS name + IP) to a group of Pods. Pods come and go, but the Service address stays constant. Uses round-robin load balancing. |
| **ConfigMap** | Stores non-secret configuration (env vars, config files) separately from the container image. |
| **Namespace** | A virtual cluster within the cluster — a way to group and isolate resources. Default namespace is `default`. |

### Resource Requests vs Limits
```yaml
resources:
  requests:
    cpu: "1"      # Kubernetes guarantees this much CPU is available
  limits:
    cpu: "1"      # Hard ceiling — process cannot use more than this
    memory: "1Gi"
```
- **Request** = what Kubernetes *reserves* when scheduling the Pod onto a node. The Pod is guaranteed this much.
- **Limit** = the hard cap. If the process tries to use more CPU, it gets throttled. If it exceeds memory limit, it gets killed (OOMKilled).
- Setting request == limit (as we do for 1 CPU) pins the Pod to exactly that resource allocation. This is what makes the experiment fair: each replica always uses exactly 1 core.

### Why We Route Directly to Pod IPs (Not Through a Service)
A Kubernetes Service load-balances with simple round-robin — it has no idea which Pod is currently busy vs idle. If you send a request to the Service and all Pods are busy, it picks one at random and the request stacks up inside that Pod's process queue. This breaks our requirement: "each replica processes exactly one query at a time."

By tracking each Pod IP individually and only routing to *known-free* Pods, the Dispatcher enforces single-request-per-replica at the network level.

### The Kubernetes API Server
The control plane's front door. Every interaction with the cluster goes through it — `kubectl` talks to it, your autoscaler talks to it, Deployments are managed through it. It exposes a REST API. To change the number of replicas in a Deployment, you send a PATCH request to:
```
PATCH /apis/apps/v1/namespaces/default/deployments/ml-replica
Body: {"spec": {"replicas": 5}}
```
The Python `kubernetes` library wraps this into clean function calls so you don't write raw HTTP.

---

## ML Serving Patterns

### Load the Model Once at Startup
Loading ResNet18 from disk takes ~1–2 seconds and allocates ~45MB of memory for weights. If you loaded it per-request, every inference would be 10× slower. The correct pattern is to load once when the server starts, keep the model object in a module-level variable, and reuse it for every request. This is standard practice in all ML serving systems.

```python
# At module level (runs once when uvicorn imports the app)
model = resnet18(weights=ResNet18_Weights.IMAGENET1K_V1)
model.eval()  # switches off training-only behavior (dropout, batchnorm updates)
```

### `model.eval()` — Why It Matters
PyTorch models have two modes:
- **Training mode** (`model.train()`): dropout randomly zeros activations, batchnorm updates running statistics. This introduces randomness — intentional during training to prevent overfitting.
- **Inference mode** (`model.eval()`): dropout is disabled, batchnorm uses frozen statistics. Deterministic, faster, and correct for serving.

Always call `model.eval()` before serving predictions.

### `torch.no_grad()` — Why It Matters
During inference, you don't need PyTorch to track gradients (gradients are only needed for backpropagation during training). Wrapping inference in `torch.no_grad()` disables gradient tracking, which reduces memory usage by ~30–50% and speeds up inference.

```python
with torch.no_grad():
    output = model(image_tensor)
```

### Enforcing Single-Request-at-a-Time with `--workers 1`
```bash
uvicorn app:app --host 0.0.0.0 --port 8080 --workers 1
```
`--workers 1` = one OS process, one thread, handles one request at a time. uvicorn may queue additional requests internally, but our Dispatcher prevents that by only sending to free replicas. The `--workers 1` flag is a safety net.

### CPU-Only PyTorch Build
```dockerfile
RUN pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
```
The default `pip install torch` downloads a ~700MB GPU-capable build. The CPU-only build is ~200MB. On Minikube where image pulls are slow and disk space is limited, always use the CPU build when you don't have a GPU.

---

## Prometheus & Metrics

### What Prometheus Is
Prometheus is a time-series metrics database. It works on a **pull model**: your application exposes a `/metrics` HTTP endpoint in a specific text format, and Prometheus scrapes (fetches) that endpoint on a schedule (e.g. every 15 seconds). It stores the values with timestamps, and you can query them with PromQL (Prometheus Query Language).

### The Four Metric Types

| Type | Description | Example use |
|---|---|---|
| **Counter** | Monotonically increasing number. Never resets (except on restart). | Total requests served, total errors |
| **Gauge** | A value that goes up and down. | Current queue depth, current replica count |
| **Histogram** | Samples observations into configurable buckets. Gives you percentiles. | Request duration (p50, p95, p99 latency) |
| **Summary** | Similar to Histogram but percentiles computed client-side. Less flexible. | Rarely used in modern setups |

### Computing Rate from a Counter
Counters only go up, so you compute rate by taking the difference between two scrapes:
```
rate = (counter_now - counter_15s_ago) / 15
```
In PromQL: `rate(requests_total[1m])` — average per-second rate over the last 1 minute.
In the autoscaler Python code, you do this manually: query Prometheus for the counter value twice, divide the delta by the interval.

### Why Prometheus Uses Pull (Not Push)
With pull, Prometheus controls the scrape schedule and knows exactly which targets are alive (it can alert if a target goes missing). With push, you'd need to configure each application to know where to send metrics. Pull also makes it easy to add new metrics sources — just register the scrape target in Prometheus config.

---

## Queueing Theory

### Little's Law
The fundamental theorem of queueing theory:

> **L = λ × W**

Where:
- **L** = average number of items in the system (queue + being served)
- **λ** (lambda) = average arrival rate (requests/second)
- **W** = average time an item spends in the system (service time in seconds)

Applied to our autoscaler:
- Each replica is one "server" that handles one request at a time
- If arrival rate = 20 req/s and mean inference time = 200ms (0.2s):
  - Work required = 20 × 0.2 = 4 replica-seconds per second = **4 replicas** to keep up at 100% utilization
  - At 70% target utilization: `ceil(4 / 0.7)` = **6 replicas** (30% headroom for latency spikes)

### Why Little's Law Beats CPU-Based HPA
- **HPA** reacts to CPU *after* it has already spiked. There is an inherent delay: load increases → inference runs hot → CPU rises → HPA detects → scales → new Pods start (~30s). During that window, latency spikes.
- **Little's Law autoscaler** reacts to *arrival rate and queue depth* — which change the moment load increases, before CPU has time to saturate. It's a **leading indicator** vs HPA's **lagging indicator**.

| Signal | Type | Lag |
|---|---|---|
| CPU utilization | Lagging | 30–60s before scaling happens |
| Queue depth | Leading | Reacts within one 15s poll cycle |
| Arrival rate | Leading | Reacts within one 15s poll cycle |

---

## Docker & Containerization

### Why Containers?
A container packages your application *and* all its dependencies (Python version, libraries, OS tools) into a single image. On any machine that runs Docker, the container behaves identically. This solves "works on my machine" problems and is why Kubernetes can run your app on any node in the cluster.

### Dockerfile Key Instructions
```dockerfile
FROM python:3.11-slim    # base image — slim = smaller, no dev tools
WORKDIR /app             # all subsequent commands run from /app
COPY requirements.txt .  # copy first, so pip install is cached if requirements don't change
RUN pip install ...      # runs at build time, baked into the image
COPY app.py .            # copy source last — changes here don't invalidate pip cache
CMD [...]                # default command when container starts
```

**Layer caching:** Docker builds images in layers. If a layer hasn't changed, Docker reuses the cached version. Copying `requirements.txt` and running `pip install` *before* copying your source code means that editing `app.py` doesn't re-run `pip install` — only the last `COPY` layer and `CMD` re-run. This makes rebuilds fast.

---

## The Dispatcher

### Connection-Level vs Application-Level Load Balancing

A Kubernetes **Service** does connection-level load balancing — round-robin across all Pods without knowing whether a Pod is currently busy. It may send a new request to a Pod already processing a 3-second inference while another Pod is completely idle.

The Dispatcher does **application-level** load balancing — it tracks which Pods are free at the request level and only routes to a Pod that has just finished its previous request. This gives better utilization and more predictable latency, at the cost of needing to build and operate the Dispatcher ourselves.

### The Kubernetes Watch API

Instead of polling the API Server every N seconds to discover Pod IPs, the Dispatcher opens a **Watch** — a long-lived HTTP connection to the Kubernetes API Server that streams events in real time:

| Event | Meaning |
|---|---|
| `ADDED` | A new Pod was created |
| `MODIFIED` | A Pod's status changed (e.g., became `Running`) |
| `DELETED` | A Pod was removed |

The Dispatcher maintains a live set of ready Pod IPs by processing these events. When the autoscaler scales up and a new Pod reaches `Running` state, the Dispatcher sees the `MODIFIED` event and immediately adds that IP to its routing pool. No polling, no stale data. We use the `kubernetes-asyncio` Python library which wraps this API cleanly.

### asyncio — I/O-Bound Concurrency Without Threads

The Dispatcher must handle many concurrent tasks: receiving HTTP requests, watching Kubernetes events, forwarding requests to replica Pods, waiting for responses. These are all **I/O-bound** — they spend most of their time waiting on the network, not burning CPU.

Python's `asyncio` lets a **single thread** manage thousands of in-flight operations by switching between them whenever one is waiting on I/O. This is called **cooperative multitasking** via `async/await`. The alternative — spawning a thread per request — would have much higher overhead and locking complexity for what is fundamentally a network proxy.

```python
async def handle_request(request):
    replica_ip = await get_free_replica()   # yields while waiting
    response = await forward(replica_ip, request)  # yields while waiting
    return response
```

### Free-Replica Routing and Backpressure

The Dispatcher enforces **single-request-per-replica** at the network level:

1. Maintain a set of free replica IPs
2. When a request arrives, pop a free replica from the set
3. Forward the request; when the response comes back, return the replica to the free set
4. While a replica is busy it is absent from the free set — no double-booking

If no replicas are free, the request waits in an **asyncio queue** (bounded by `MAX_QUEUE_DEPTH`). If the queue is also full, the request is dropped immediately with **HTTP 503 Service Unavailable**. This is **backpressure** — the system refuses to accept more load than it can handle rather than letting latency grow unboundedly.

### Dispatcher Metrics

| Metric | Type | What it measures |
|---|---|---|
| `dispatcher_queue_depth` | Gauge | Requests currently waiting in queue |
| `dispatcher_dropped_requests_total` | Counter | Requests rejected (queue full → 503) |
| `dispatcher_requests_total` | Counter | All requests received |
| `dispatcher_request_duration_seconds` | Histogram | End-to-end latency (client → dispatcher → replica → back) |
| `dispatcher_active_replicas` | Gauge | Number of replica IPs currently tracked as ready |

The autoscaler uses `dispatcher_requests_total` to compute arrival rate (λ) and the replica's `inference_duration_seconds` for mean service time (W) to run Little's Law.

---

## The Autoscaler

### The Control Loop Pattern

The Autoscaler runs a continuous **observe → analyze → act → wait** loop every 15 seconds:

1. **Observe** — query Prometheus for current metrics and Kubernetes for current replica count
2. **Analyze** — compute how many replicas are needed using Little's Law
3. **Act** — if desired ≠ current, PATCH the Deployment
4. **Wait** — sleep 15s, then repeat

The loop never assumes its last action succeeded — it always re-reads actual state. This makes it **self-healing**: if a Pod crashes between cycles, the next cycle detects the drift and corrects it automatically. This pattern appears everywhere in Kubernetes internals (it's how Deployments, ReplicaSets, and the built-in HPA all work).

### Little's Law: Computing Desired Replicas from Live Metrics

The autoscaler computes three values from Prometheus each cycle:

**Arrival rate (λ)** — from the `dispatcher_requests_total` counter:
```
λ = (requests_total_now − requests_total_15s_ago) / 15   # requests/second
```

**Mean service time (W)** — from the `inference_duration_seconds` histogram:
```
W = (duration_sum_now − duration_sum_15s_ago) / (duration_count_now − duration_count_15s_ago)
```

**Desired replicas:**
```python
desired = math.ceil((λ * W) / TARGET_UTILIZATION)   # TARGET_UTILIZATION = 0.7
desired = max(MIN_REPLICAS, min(MAX_REPLICAS, desired))
```

### Why Target 70% Utilization (Not 100%)?

At 100% utilization, queueing theory shows that latency becomes unbounded — any small burst causes requests to pile up with no spare capacity to absorb it. The relationship is non-linear: going from 80% → 90% roughly doubles queue depth; 90% → 95% doubles it again. Staying at 70% keeps the system in a stable, predictable zone with 30% headroom for natural traffic variance.

### Cooldowns: Scale-Up Fast, Scale-Down Slow

Without cooldowns, natural variance in arrival rate causes the autoscaler to oscillate — scaling up, then down, then up again within minutes. Pod startup takes ~10–20 seconds, so constant churn wastes resources and creates instability. We use **asymmetric cooldowns**:

| Direction | Cooldown | Reasoning |
|---|---|---|
| Scale up | 30s | Fast — a user is waiting right now, latency is hurting |
| Scale down | 3 minutes | Slow — wait for the burst to actually be over |

Implementation: record the timestamp of the last scale event in each direction; before acting, check elapsed time.

### Replica Bounds

| Parameter | Value | Why |
|---|---|---|
| `MIN_REPLICAS` | 1 | Keep one replica warm — avoids cold-start latency on the first request |
| `MAX_REPLICAS` | 10 | Minikube runs on a laptop — cap it to avoid exhausting the node |

### Kubernetes RBAC — Giving the Autoscaler Permission

To PATCH a Deployment, the Autoscaler Pod needs explicit permission. Kubernetes uses **RBAC (Role-Based Access Control)** for this:

- **Role** — a set of allowed actions on specific resource types (e.g. "patch deployments")
- **ServiceAccount** — an identity assigned to a Pod (like a user account, but for Pods)
- **RoleBinding** — links a Role to a ServiceAccount

Kubernetes automatically mounts the ServiceAccount's credentials into the Pod. The `kubernetes` Python library picks them up automatically with `config.load_incluster_config()` — no auth code needed.

### ServiceAccount and In-Cluster Config

```python
from kubernetes import client, config

config.load_incluster_config()   # reads auto-mounted credentials from the Pod filesystem
apps_v1 = client.AppsV1Api()
apps_v1.patch_namespaced_deployment_scale(
    name="ml-replica",
    namespace="default",
    body={"spec": {"replicas": desired}}
)
```

> **Simpler choice for this project:** We create a single Role + RoleBinding manually in a YAML manifest. In production, teams use tools like Helm or OPA (Open Policy Agent) to manage RBAC at scale with audit trails. For one autoscaler with one permission, a static manifest is cleaner and easier to read.

---

## Prometheus Monitoring Setup

### How Prometheus Discovers Scrape Targets

Prometheus needs to know what IP:port to hit for each `/metrics` endpoint. There are two approaches:

**Static config (what we use):**
```yaml
scrape_configs:
  - job_name: 'dispatcher'
    static_configs:
      - targets: ['dispatcher-service:9090']
  - job_name: 'ml-replicas'
    static_configs:
      - targets: ['ml-replica-service:9090']
```
Prometheus scrapes the Kubernetes Service DNS name. The Service load-balances to whichever Pod it picks.

> **Simpler choice for this project:** The production approach is **Kubernetes service discovery** — Prometheus watches the Kubernetes API itself and auto-discovers Pods by label, updating its target list as Pods come and go. This handles dynamic fleets automatically. We use static config instead because our service names are fixed and known in advance, and it's far easier to read and debug. Service discovery adds meaningful complexity for no gain here.

### The `/metrics` Text Format

When Prometheus scrapes your app, it gets a plain-text response:
```
# HELP inference_duration_seconds Time to run one inference
# TYPE inference_duration_seconds histogram
inference_duration_seconds_bucket{le="0.1"} 12
inference_duration_seconds_bucket{le="0.25"} 847
inference_duration_seconds_bucket{le="+Inf"} 1024
inference_duration_seconds_sum 198.4
inference_duration_seconds_count 1024
dispatcher_queue_depth 3
```
Each line is a **time series** — metric name + labels form a unique key, and Prometheus records the value at every scrape timestamp. Histograms automatically emit `_sum`, `_count`, and `_bucket` series. The `prometheus-client` Python library handles generating this format for you.

### Scraping Pods via Service vs Per-Pod

When you scrape the Service (not individual Pod IPs), Prometheus hits whichever Pod the Service routes it to — different Pods on different scrapes. This means the `_sum` and `_count` values you see may come from different Pods each time.

> **Simpler choice for this project:** The production approach is to scrape each Pod IP directly with a Pod-level label (e.g. `pod="ml-replica-abc123"`) so you get per-Pod breakdowns and consistent counters. We scrape via Service instead because we only need *aggregate* metrics (total requests across all replicas, mean inference time across all replicas). The delta computation in the autoscaler (`value_now − value_15s_ago`) cancels out any Pod-level inconsistency for aggregate rates. Adding per-Pod scraping would complicate the Prometheus config without improving the experiment results.

### Installing Prometheus with Helm

**Helm** is the Kubernetes package manager. Think of it as `pip` for Kubernetes — it bundles all the YAML manifests for a complex application into a single installable package called a **chart**.

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install prometheus prometheus-community/kube-prometheus-stack \
  --set prometheus.prometheusSpec.scrapeInterval=15s
```

This installs Prometheus + Grafana (a dashboard UI) with one command instead of manually writing dozens of YAML manifests.

### Grafana Queries for the Experiment

Grafana connects to Prometheus and plots time-series dashboards. Key PromQL queries for the experiment comparison:

| Panel | PromQL | What it shows |
|---|---|---|
| Request rate | `rate(dispatcher_requests_total[1m])` | Incoming load over time |
| p99 latency | `histogram_quantile(0.99, rate(inference_duration_seconds_bucket[1m]))` | Worst-case latency |
| Replica count | `kube_deployment_spec_replicas{deployment="ml-replica"}` | Scaling behavior |
| Queue depth | `dispatcher_queue_depth` | Backpressure signal |
| Dropped requests | `rate(dispatcher_dropped_requests_total[1m])` | 503s per second |

`histogram_quantile(0.99, ...)` computes the p99 from the histogram buckets — the value below which 99% of all requests fall. This is the standard way to measure tail latency.

---

## Repo Structure & Kubernetes Manifests

### Separating Application Code from Kubernetes Manifests

A Kubernetes project has two distinct kinds of files: **application code** (Python, Dockerfiles) and **Kubernetes manifests** (YAML describing what runs in the cluster). Keeping them in separate trees makes it easy to answer "where do I look to change X?" One directory per component for code; a dedicated `k8s/` tree for all manifests.

### Anatomy of a Deployment Manifest

A `deployment.yaml` tells Kubernetes "run N copies of this container image and keep them alive":

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ml-replica
spec:
  replicas: 1                        # autoscaler changes this at runtime
  selector:
    matchLabels:
      app: ml-replica                # Deployment owns Pods with this label
  template:
    metadata:
      labels:
        app: ml-replica
    spec:
      containers:
        - name: ml-replica
          image: ml-replica:latest
          imagePullPolicy: Never     # use Minikube's local image, not Docker Hub
          resources:
            requests: { cpu: "1", memory: "1Gi" }
            limits:   { cpu: "1", memory: "1Gi" }
```

### Anatomy of a Service Manifest

A `service.yaml` gives a group of Pods a stable DNS name inside the cluster. Pods are ephemeral (they get new IPs when restarted), but the Service address never changes:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: ml-replica-service
spec:
  selector:
    app: ml-replica    # routes to all Pods with this label
  ports:
    - port: 8080
      targetPort: 8080
```

### Minikube Image Builds — Why `imagePullPolicy: Never`

Kubernetes pulls container images from a registry (e.g. Docker Hub) by default. During development we don't want to push to Docker Hub on every change. Minikube has its own Docker daemon running inside the VM — if you build your image into *that* daemon, Kubernetes finds it locally:

```bash
eval $(minikube docker-env)        # point your shell at Minikube's Docker daemon
docker build -t ml-replica:latest ./ml_replica
```

Setting `imagePullPolicy: Never` in the manifest tells Kubernetes: don't try to pull this from the internet — use what's already local.

> **Simpler choice for this project:** Production teams push images to a private registry (ECR, GCR, etc.) via CI/CD on every merge. We build directly into Minikube's daemon instead — no registry account needed, no push step, rebuilds are instant. This only works on a single-node local cluster; it would not work on a real multi-node cluster.

### The Built-in HPA Manifest

The Kubernetes HPA watches a Deployment and adjusts `spec.replicas` based on average CPU utilization across all Pods:

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: ml-replica-hpa-70
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: ml-replica
  minReplicas: 1
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70   # scale up when average CPU across all Pods exceeds 70%
```

Two manifests are kept in `k8s/hpa/` — `hpa-70.yaml` and `hpa-90.yaml`. During the experiment you `kubectl apply` one at a time and delete it before the next run.
