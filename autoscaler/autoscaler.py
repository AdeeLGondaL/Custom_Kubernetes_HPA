import math
import os
import time
import logging
import requests
from kubernetes import client, config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL",
                                 "http://prometheus-kube-prometheus-prometheus:9090")
NAMESPACE      = os.environ.get("NAMESPACE", "default")
DEPLOYMENT     = os.environ.get("DEPLOYMENT", "ml-replica")
TARGET_UTIL    = float(os.environ.get("TARGET_UTILIZATION", "0.7"))
MIN_REPLICAS   = int(os.environ.get("MIN_REPLICAS", "1"))
MAX_REPLICAS   = int(os.environ.get("MAX_REPLICAS", "10"))
SCALE_UP_CD    = float(os.environ.get("SCALE_UP_COOLDOWN", "30"))
SCALE_DOWN_CD  = float(os.environ.get("SCALE_DOWN_COOLDOWN", "180"))
POLL_INTERVAL  = float(os.environ.get("POLL_INTERVAL", "15"))


def query_prometheus(url: str, promql: str) -> float:
    resp = requests.get(f"{url}/api/v1/query", params={"query": promql}, timeout=5)
    resp.raise_for_status()
    result = resp.json()["data"]["result"]
    if not result:
        return 0.0
    return float(result[0]["value"][1])


def compute_arrival_rate(prom_url: str) -> float:
    return query_prometheus(prom_url, "rate(dispatcher_requests_total[1m])")


def compute_mean_service_time(prom_url: str) -> float:
    rate_sum   = query_prometheus(prom_url, "rate(inference_duration_seconds_sum[1m])")
    rate_count = query_prometheus(prom_url, "rate(inference_duration_seconds_count[1m])")
    if rate_count == 0.0:
        return 0.0
    return rate_sum / rate_count


def compute_desired_replicas(
    arrival_rate: float,
    service_time: float,
    target_utilization: float,
    min_replicas: int = 1,
    max_replicas: int = 10,
) -> int:
    if arrival_rate <= 0 or service_time <= 0:
        return min_replicas
    desired = math.ceil((arrival_rate * service_time) / target_utilization)
    return max(min_replicas, min(max_replicas, desired))


def get_current_replicas(apps_v1, namespace: str, deployment: str) -> int:
    scale = apps_v1.read_namespaced_deployment_scale(name=deployment, namespace=namespace)
    return scale.spec.replicas or 0


def patch_replicas(apps_v1, namespace: str, deployment: str, replicas: int) -> None:
    apps_v1.patch_namespaced_deployment_scale(
        name=deployment,
        namespace=namespace,
        body={"spec": {"replicas": replicas}},
    )
    log.info(f"Scaled {deployment} → {replicas} replicas")


def run_loop():
    config.load_incluster_config()
    apps_v1         = client.AppsV1Api()
    last_scale_up   = 0.0
    last_scale_down = 0.0

    while True:
        try:
            arrival_rate = compute_arrival_rate(PROMETHEUS_URL)
            service_time = compute_mean_service_time(PROMETHEUS_URL)
            desired      = compute_desired_replicas(arrival_rate, service_time, TARGET_UTIL, MIN_REPLICAS, MAX_REPLICAS)
            current      = get_current_replicas(apps_v1, NAMESPACE, DEPLOYMENT)
            now          = time.monotonic()

            log.info(f"λ={arrival_rate:.2f} req/s  W={service_time:.3f}s  desired={desired}  current={current}")

            if desired > current and (now - last_scale_up) >= SCALE_UP_CD:
                patch_replicas(apps_v1, NAMESPACE, DEPLOYMENT, desired)
                last_scale_up = now
            elif desired < current and (now - last_scale_down) >= SCALE_DOWN_CD:
                patch_replicas(apps_v1, NAMESPACE, DEPLOYMENT, desired)
                last_scale_down = now

        except Exception as exc:
            log.warning(f"Control loop error (will retry): {exc}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run_loop()
