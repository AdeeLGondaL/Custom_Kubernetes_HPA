"""
Export experiment metrics from Prometheus to CSV files.

Run immediately after each experiment:
    python scripts/export_metrics.py --name custom-hpa
    python scripts/export_metrics.py --name hpa-70
    python scripts/export_metrics.py --name hpa-90

CSV files are saved to results/<name>_<metric>.csv
"""

import argparse
import csv
import os
import time
import requests

METRICS = {
    # REPORT METRIC 1: end-to-end service latency (includes queue wait time)
    "latency_p99": (
        "histogram_quantile(0.99, sum(rate(dispatcher_request_duration_seconds_bucket[1m])) by (le))",
        "latency_s",
    ),
    # REPORT METRIC 2: CPU cores allocated (= replica count)
    "cpu_cores": (
        'max(kube_deployment_status_replicas_available{deployment="ml-replica"})',
        "cpu_cores",
    ),
    # Context metrics
    "request_rate": (
        "rate(dispatcher_requests_total[30s])",
        "req_per_s",
    ),
    "queue_depth": (
        "dispatcher_queue_depth",
        "queue_depth",
    ),
    "dropped_rate": (
        "rate(dispatcher_dropped_requests_total[30s])",
        "dropped_per_s",
    ),
    "inference_latency_p99": (
        "histogram_quantile(0.99, sum(rate(inference_duration_seconds_bucket[1m])) by (le))",
        "latency_s",
    ),
}

STEP = 15  # seconds — matches autoscaler poll interval


def query_range(prom_url: str, expr: str, minutes: int, offset_minutes: int = 0) -> list[dict]:
    end = time.time() - offset_minutes * 60
    start = end - minutes * 60
    resp = requests.get(
        f"{prom_url}/api/v1/query_range",
        params={"query": expr, "start": start, "end": end, "step": STEP},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    results = data.get("data", {}).get("result", [])
    if not results:
        return []
    # Take first series (metrics like these return a single stream)
    values = results[0]["values"]
    return [{"unix_ts": float(ts), "value": float(v)} for ts, v in values]


def save_csv(rows: list[dict], value_col: str, path: str) -> None:
    # Normalise timestamps: t=0 at the first data point
    if not rows:
        print(f"  WARNING: no data returned, skipping {path}")
        return
    t0 = rows[0]["unix_ts"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["unix_ts", "elapsed_s", value_col])
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "unix_ts":   row["unix_ts"],
                "elapsed_s": round(row["unix_ts"] - t0, 1),
                value_col:   row["value"],
            })
    print(f"  Saved {len(rows)} rows → {path}")


def main():
    parser = argparse.ArgumentParser(description="Export Prometheus metrics to CSV")
    parser.add_argument(
        "--name", required=True,
        choices=["custom-hpa", "hpa-70", "hpa-90"],
        help="Experiment name (used as filename prefix)",
    )
    parser.add_argument(
        "--minutes", type=int, default=11,
        help="Duration of the query window in minutes (default 11, covers the 630 s workload)",
    )
    parser.add_argument(
        "--offset-minutes", type=int, default=0,
        help="How many minutes ago the experiment ENDED (default 0 = just finished). "
             "Use this to re-export a past experiment. E.g. --offset-minutes 60 means "
             "capture the window ending 60 minutes ago.",
    )
    parser.add_argument(
        "--prom", default="http://localhost:9090",
        help="Prometheus URL (default: http://localhost:9090)",
    )
    args = parser.parse_args()

    offset = args.offset_minutes
    window_end = time.time() - offset * 60
    print(f"\nExporting metrics for experiment: {args.name}")
    print(f"Prometheus: {args.prom}")
    print(f"Window: {args.minutes} min ending {offset} min ago  "
          f"({time.strftime('%H:%M', time.localtime(window_end - args.minutes*60))} → "
          f"{time.strftime('%H:%M', time.localtime(window_end))})\n")

    out_dir = os.path.join("results", args.name)
    for metric_key, (expr, col) in METRICS.items():
        print(f"Querying: {metric_key}")
        try:
            rows = query_range(args.prom, expr, args.minutes, offset)
            path = os.path.join(out_dir, f"{metric_key}.csv")
            save_csv(rows, col, path)
        except Exception as exc:
            print(f"  ERROR: {exc}")

    print(f"\nDone. Files saved to results/{args.name}/")
    print("Next: run  python scripts/plot_comparison.py  after all 3 experiments.")


if __name__ == "__main__":
    main()
