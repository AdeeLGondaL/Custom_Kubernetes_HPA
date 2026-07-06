import asyncio
import os
import time
import httpx
from fastapi import FastAPI, File, UploadFile, Response
from prometheus_client import Counter, Gauge, Histogram, make_asgi_app

MAX_QUEUE_DEPTH = int(os.environ.get("MAX_QUEUE_DEPTH", "50"))
NAMESPACE       = os.environ.get("NAMESPACE", "default")
POD_LABEL       = os.environ.get("POD_LABEL", "app=ml-replica")
REPLICA_PORT    = int(os.environ.get("REPLICA_PORT", "8080"))

_queue_depth     = Gauge("dispatcher_queue_depth", "Requests waiting in queue")
_dropped         = Counter("dispatcher_dropped_requests_total", "Requests dropped with 503")
_received        = Counter("dispatcher_requests_total", "Total requests received")
_duration        = Histogram("dispatcher_request_duration_seconds", "End-to-end latency",
                              buckets=[0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0])
_active_replicas = Gauge("dispatcher_active_replicas", "Ready replica IPs tracked")

valid_ips: set          = set()
free_queue: asyncio.Queue | None = None
pending_count: int      = 0
_in_use: set            = set()

app = FastAPI()
app.mount("/metrics", make_asgi_app())


def _init_state():
    global free_queue, pending_count, valid_ips, _in_use
    free_queue    = asyncio.Queue()
    pending_count = 0
    valid_ips     = set()
    _in_use       = set()


async def on_pod_event(event_type: str, ip: str, is_ready: bool):
    if event_type in ("ADDED", "MODIFIED") and is_ready:
        if ip not in valid_ips:
            valid_ips.add(ip)
            await free_queue.put(ip)
            _active_replicas.set(len(valid_ips))
    elif event_type == "DELETED" or (event_type == "MODIFIED" and not is_ready):
        valid_ips.discard(ip)
        _active_replicas.set(len(valid_ips))


async def _get_free_replica() -> str:
    while True:
        ip = await free_queue.get()
        if ip in valid_ips and ip not in _in_use:
            _in_use.add(ip)
            return ip


async def _release_replica(ip: str):
    _in_use.discard(ip)
    if ip in valid_ips:
        await free_queue.put(ip)


def _mark_replica_unavailable(ip: str):
    _in_use.discard(ip)
    valid_ips.discard(ip)
    _active_replicas.set(len(valid_ips))


@app.on_event("startup")
async def startup():
    if free_queue is None:
        _init_state()
    if os.environ.get("DISABLE_K8S_WATCH") != "1":
        asyncio.create_task(_watch_pods())


async def _watch_pods():
    from kubernetes_asyncio import client as k8s, config, watch
    config.load_incluster_config()
    v1 = k8s.CoreV1Api()
    w  = watch.Watch()
    async for event in w.stream(v1.list_namespaced_pod,
                                 namespace=NAMESPACE,
                                 label_selector=POD_LABEL):
        pod        = event["object"]
        event_type = event["type"]
        ip         = pod.status.pod_ip
        if not ip:
            continue
        is_ready = (
            pod.status.phase == "Running"
            and any(
                c.type == "Ready" and c.status == "True"
                for c in (pod.status.conditions or [])
            )
        )
        await on_pod_event(event_type, ip, is_ready)


@app.post("/infer")
async def infer(file: UploadFile = File(...)):
    global pending_count
    _received.inc()

    if pending_count >= MAX_QUEUE_DEPTH:
        _dropped.inc()
        return Response(status_code=503, content="Queue full")

    pending_count += 1
    _queue_depth.set(pending_count)
    t_start    = time.perf_counter()
    replica_ip = None

    try:
        image_bytes = await file.read()
        attempts = max(1, min(3, len(valid_ips)))

        for _ in range(attempts):
            replica_ip = await _get_free_replica()
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        f"http://{replica_ip}:{REPLICA_PORT}/infer",
                        files={"file": ("image.jpg", image_bytes, "image/jpeg")},
                    )
                _duration.observe(time.perf_counter() - t_start)
                return Response(content=resp.content, status_code=resp.status_code,
                                media_type="application/json")
            except httpx.RequestError:
                _mark_replica_unavailable(replica_ip)
                replica_ip = None

        _dropped.inc()
        return Response(status_code=503, content="No reachable replica")
    finally:
        if replica_ip is not None:
            await _release_replica(replica_ip)
        pending_count -= 1
        _queue_depth.set(pending_count)


@app.get("/health")
async def health():
    return {"status": "ok", "replicas": len(valid_ips)}
