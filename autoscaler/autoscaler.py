import logging
import math
import os
import time

import requests
from kubernetes import client, config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

PROMETHEUS_URL = os.environ.get(
    "PROMETHEUS_URL",
    "http://prometheus-kube-prometheus-prometheus:9090",
)
NAMESPACE = os.environ.get("NAMESPACE", "default")
DEPLOYMENT = os.environ.get("DEPLOYMENT", "ml-replica")
TARGET_UTIL = float(os.environ.get("TARGET_UTILIZATION", "0.7"))
MIN_REPLICAS = int(os.environ.get("MIN_REPLICAS", "2"))
MAX_REPLICAS = int(os.environ.get("MAX_REPLICAS", "10"))
SCALE_UP_CD = float(os.environ.get("SCALE_UP_COOLDOWN", "15"))
SCALE_DOWN_CD = float(os.environ.get("SCALE_DOWN_COOLDOWN", "180"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "15"))

METRIC_WINDOW = "30s"
MAX_SCALE_UP_STEP = 2
MAX_SCALE_DOWN_STEP = 1


def query_prometheus(url: str, promql: str) -> float:
    resp = requests.get(f"{url}/api/v1/query", params={"query": promql}, timeout=5)
    resp.raise_for_status()
    result = resp.json()["data"]["result"]
    if not result:
        return 0.0
    return float(result[0]["value"][1])


def compute_arrival_rate(prom_url: str) -> float:
    return query_prometheus(prom_url, f"rate(dispatcher_requests_total[{METRIC_WINDOW}])")


def compute_mean_service_time(prom_url: str) -> float:
    rate_sum = query_prometheus(prom_url, f"rate(inference_duration_seconds_sum[{METRIC_WINDOW}])")
    rate_count = query_prometheus(prom_url, f"rate(inference_duration_seconds_count[{METRIC_WINDOW}])")
    if rate_count == 0.0:
        return 0.0
    return rate_sum / rate_count


def compute_queue_depth(prom_url: str) -> float:
    return query_prometheus(prom_url, "dispatcher_queue_depth")


def compute_desired_replicas(
    arrival_rate: float,
    service_time: float,
    target_utilization: float,
    min_replicas: int = 1,
    max_replicas: int = 10,
    queue_depth: float = 0.0,
) -> int:
    if service_time <= 0:
        return min_replicas

    load_based = math.ceil((max(0.0, arrival_rate) * service_time) / target_utilization)
    if queue_depth > 0:
        load_based += 1

    desired = max(min_replicas, load_based)
    return max(min_replicas, min(max_replicas, desired))


def limit_replica_step(
    current: int,
    desired: int,
    max_scale_up_step: int = MAX_SCALE_UP_STEP,
    max_scale_down_step: int = MAX_SCALE_DOWN_STEP,
) -> int:
    if desired > current:
        return min(desired, current + max_scale_up_step)
    if desired < current:
        return max(desired, current - max_scale_down_step)
    return desired


def apply_queue_pressure(
    current: int,
    desired: int,
    queue_depth: float,
    max_replicas: int,
    max_scale_up_step: int = MAX_SCALE_UP_STEP,
) -> int:
    if queue_depth <= 0:
        return desired
    queue_target = min(max_replicas, current + max_scale_up_step)
    return max(desired, queue_target)


def get_current_replicas(apps_v1, namespace: str, deployment: str) -> int:
    scale = apps_v1.read_namespaced_deployment_scale(name=deployment, namespace=namespace)
    return scale.spec.replicas or 0


def patch_replicas(apps_v1, namespace: str, deployment: str, replicas: int) -> None:
    apps_v1.patch_namespaced_deployment_scale(
        name=deployment,
        namespace=namespace,
        body={"spec": {"replicas": replicas}},
    )
    log.info("Scaled %s to %s replicas", deployment, replicas)


def run_loop():
    config.load_incluster_config()
    apps_v1 = client.AppsV1Api()
    last_scale_up = 0.0
    last_scale_down = 0.0

    while True:
        try:
            arrival_rate = compute_arrival_rate(PROMETHEUS_URL)
            service_time = compute_mean_service_time(PROMETHEUS_URL)
            queue_depth = compute_queue_depth(PROMETHEUS_URL)
            raw_desired = compute_desired_replicas(
                arrival_rate,
                service_time,
                TARGET_UTIL,
                MIN_REPLICAS,
                MAX_REPLICAS,
                queue_depth,
            )
            current = get_current_replicas(apps_v1, NAMESPACE, DEPLOYMENT)
            raw_desired = apply_queue_pressure(current, raw_desired, queue_depth, MAX_REPLICAS)
            desired = limit_replica_step(current, raw_desired)
            now = time.monotonic()

            log.info(
                "lambda=%.2f req/s W=%.3fs queue=%.0f raw_desired=%s "
                "desired=%s current=%s",
                arrival_rate,
                service_time,
                queue_depth,
                raw_desired,
                desired,
                current,
            )

            if desired > current and (now - last_scale_up) >= SCALE_UP_CD:
                patch_replicas(apps_v1, NAMESPACE, DEPLOYMENT, desired)
                last_scale_up = now
            elif (
                desired < current
                and (now - last_scale_down) >= SCALE_DOWN_CD
                and (now - last_scale_up) >= SCALE_DOWN_CD
            ):
                patch_replicas(apps_v1, NAMESPACE, DEPLOYMENT, desired)
                last_scale_down = now

        except Exception as exc:
            log.warning("Control loop error (will retry): %s", exc)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run_loop()
