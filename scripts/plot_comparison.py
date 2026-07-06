"""
Plot the 3-experiment comparison figures for the final report.

Run after all 3 experiments have been exported:
    python scripts/plot_comparison.py

Produces:
    results/comparison_latency_p99.png
    results/comparison_cpu_cores.png
    results/comparison_combined.png   ← use this one in the report
"""

import csv
import os
import sys
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np

matplotlib.rcParams.update({
    "font.family":     "DejaVu Sans",
    "font.size":       11,
    "axes.titlesize":  12,
    "axes.labelsize":  11,
    "legend.fontsize": 10,
    "lines.linewidth": 2,
    "grid.alpha":      0.3,
})

SLO_S = 0.5  # seconds — professor's requirement

EXPERIMENTS = [
    ("custom-hpa", "Custom HPA (queueing theory)",     "#1f77b4"),
    ("hpa-70",     "Kubernetes HPA  70 % CPU target",  "#ff7f0e"),
    ("hpa-90",     "Kubernetes HPA  90 % CPU target",  "#2ca02c"),
]


def load_csv(path: str, value_col: str) -> tuple[list, list]:
    """Return (elapsed_seconds, values) from a CSV file."""
    xs, ys = [], []
    if not os.path.exists(path):
        return xs, ys
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                xs.append(float(row["elapsed_s"]))
                ys.append(float(row[value_col]) if row[value_col] not in ("", "NaN", "nan") else float("nan"))
            except (KeyError, ValueError):
                pass
    return xs, ys


def _align_to_workload(xs: list, ys: list, total_s: int = 630) -> tuple[list, list]:
    """Shift t=0 to the first data point and trim to total_s."""
    if not xs:
        return xs, ys
    offset = xs[0]
    xs_new = [x - offset for x in xs]
    # Trim to workload duration + small tail
    pairs = [(x, y) for x, y in zip(xs_new, ys) if x <= total_s + 60]
    if not pairs:
        return xs_new, ys
    return [p[0] for p in pairs], [p[1] for p in pairs]


def plot_latency(ax: plt.Axes) -> None:
    ax.set_title("p99 Server-Side Inference Latency")
    ax.set_ylabel("Latency (s)")
    ax.set_xlabel("Time (s)")
    ax.axhline(SLO_S, color="red", linestyle="--", linewidth=1.5, label=f"SLO target ({SLO_S} s)", zorder=5)
    ax.axhspan(0, SLO_S, alpha=0.04, color="green")

    any_data = False
    for name, label, color in EXPERIMENTS:
        path = os.path.join("results", name, "latency_p99.csv")
        xs, ys = load_csv(path, "latency_s")
        xs, ys = _align_to_workload(xs, ys)
        if not xs:
            print(f"  WARNING: no latency data for {name}")
            continue
        ax.plot(xs, ys, label=label, color=color)
        any_data = True

    if not any_data:
        ax.text(0.5, 0.5, "No data found\n(run export_metrics.py first)",
                ha="center", va="center", transform=ax.transAxes, color="grey")

    ax.set_ylim(bottom=0)
    ax.yaxis.set_major_formatter(ticker.FormatStrFormatter("%.2f s"))
    ax.grid(True, axis="y")
    ax.legend(loc="upper left")


def plot_cpu_cores(ax: plt.Axes) -> None:
    ax.set_title("CPU Cores Allocated (= Replica Count)")
    ax.set_ylabel("CPU Cores")
    ax.set_xlabel("Time (s)")

    any_data = False
    for name, label, color in EXPERIMENTS:
        path = os.path.join("results", name, "cpu_cores.csv")
        xs, ys = load_csv(path, "cpu_cores")
        xs, ys = _align_to_workload(xs, ys)
        if not xs:
            print(f"  WARNING: no CPU core data for {name}")
            continue
        ax.step(xs, ys, label=label, color=color, where="post")
        any_data = True

    if not any_data:
        ax.text(0.5, 0.5, "No data found\n(run export_metrics.py first)",
                ha="center", va="center", transform=ax.transAxes, color="grey")

    ax.set_ylim(bottom=0)
    ax.yaxis.set_major_locator(ticker.MaxNLocator(integer=True))
    ax.grid(True, axis="y")
    ax.legend(loc="upper left")
    ax.annotate("all start at\n2 replicas", xy=(0, 2), xytext=(30, 1.2),
                fontsize=8, color="grey",
                arrowprops=dict(arrowstyle="->", color="grey", lw=0.8))


def plot_request_rate(ax: plt.Axes) -> None:
    ax.set_title("Incoming Request Rate (Workload)")
    ax.set_ylabel("req/s")
    ax.set_xlabel("Time (s)")

    any_data = False
    for name, label, color in EXPERIMENTS:
        path = os.path.join("results", name, "request_rate.csv")
        xs, ys = load_csv(path, "req_per_s")
        xs, ys = _align_to_workload(xs, ys)
        if not xs:
            continue
        ax.plot(xs, ys, label=label, color=color, alpha=0.7)
        any_data = True

    if not any_data:
        ax.text(0.5, 0.5, "No data found\n(run export_metrics.py first)",
                ha="center", va="center", transform=ax.transAxes, color="grey")

    ax.set_ylim(bottom=0)
    ax.grid(True, axis="y")
    ax.legend(loc="upper left")


def plot_dropped_requests(ax: plt.Axes) -> None:
    ax.set_title("Dropped Requests Rate (503s)")
    ax.set_ylabel("Dropped req/s")
    ax.set_xlabel("Time (s)")

    any_data = False
    for name, label, color in EXPERIMENTS:
        path = os.path.join("results", name, "dropped_rate.csv")
        xs, ys = load_csv(path, "dropped_per_s")
        xs, ys = _align_to_workload(xs, ys)
        if not xs:
            print(f"  WARNING: no dropped rate data for {name}")
            continue
        ax.fill_between(xs, ys, alpha=0.25, color=color, step="post")
        ax.step(xs, ys, label=label, color=color, where="post")
        any_data = True

    if not any_data:
        ax.text(0.5, 0.5, "No data found\n(run export_metrics.py first)",
                ha="center", va="center", transform=ax.transAxes, color="grey")

    ax.set_ylim(bottom=0)
    ax.grid(True, axis="y")
    ax.legend(loc="upper left")


def add_workload_shading(ax: plt.Axes) -> None:
    """Shade the spike phase (t=254–420 s) as a visual reference."""
    ax.axvspan(254, 420, alpha=0.06, color="grey", label="_spike phase")


def _first_nonnan(ys: list) -> float:
    for y in ys:
        if not (isinstance(y, float) and np.isnan(y)):
            return y
    return float("nan")


def _last_nonnan(ys: list) -> float:
    for y in reversed(ys):
        if not (isinstance(y, float) and np.isnan(y)):
            return y
    return float("nan")


def generate_summary_table() -> None:
    """Save a start-vs-end summary table as PNG for the report."""
    rows = []
    for name, label, _ in EXPERIMENTS:
        lat_xs, lat_ys = load_csv(os.path.join("results", name, "latency_p99.csv"), "latency_s")
        cpu_xs, cpu_ys = load_csv(os.path.join("results", name, "cpu_cores.csv"), "cpu_cores")
        lat_xs, lat_ys = _align_to_workload(lat_xs, lat_ys)
        cpu_xs, cpu_ys = _align_to_workload(cpu_xs, cpu_ys)
        rows.append({
            "label":      label,
            "lat_start":  _first_nonnan(lat_ys),
            "lat_end":    _last_nonnan(lat_ys),
            "cpu_start":  _first_nonnan(cpu_ys),
            "cpu_end":    _last_nonnan(cpu_ys),
        })

    if not rows:
        print("No data for summary table.")
        return

    fig, ax = plt.subplots(figsize=(10, 2.2))
    ax.axis("off")

    headers = ["Autoscaler", "p99 Latency\n(start)", "p99 Latency\n(end)", "CPU Cores\n(start)", "CPU Cores\n(end)"]
    cell_data = []
    for r in rows:
        def fmt_lat(v):
            return f"{v*1000:.0f} ms" if not np.isnan(v) else "—"
        def fmt_cpu(v):
            return f"{int(v)}" if not np.isnan(v) else "—"
        cell_data.append([
            r["label"],
            fmt_lat(r["lat_start"]),
            fmt_lat(r["lat_end"]),
            fmt_cpu(r["cpu_start"]),
            fmt_cpu(r["cpu_end"]),
        ])

    colors = [["#f0f0f0"] * 5] * len(rows)
    tbl = ax.table(cellText=cell_data, colLabels=headers,
                   loc="center", cellLoc="center")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(10)
    tbl.scale(1, 2)
    for (row, col), cell in tbl.get_celld().items():
        if row == 0:
            cell.set_facecolor("#2c3e50")
            cell.set_text_props(color="white", fontweight="bold")
        elif col == 0:
            cell.set_facecolor("#eaf0fb")
        else:
            cell.set_facecolor("#f9f9f9")
        cell.set_edgecolor("#cccccc")

    fig.suptitle("Start-of-Run vs End-of-Run Summary", fontsize=12, fontweight="bold", y=0.98)
    fig.tight_layout()
    for ext in ("png", "pdf"):
        out = os.path.join("results", f"summary_table.{ext}")
        fig.savefig(out, dpi=150, bbox_inches="tight")
        print(f"Saved: {out}")
    plt.close(fig)


def main() -> None:
    os.makedirs("results", exist_ok=True)

    missing = [n for n, _, _ in EXPERIMENTS
               if not os.path.isdir(os.path.join("results", n))]
    if missing:
        print(f"No results yet for: {', '.join(missing)}")
        print("Run  python scripts/export_metrics.py --name <name>  after each experiment.")
        if len(missing) == 3:
            sys.exit(1)

    # ── Individual plots ──────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(10, 4))
    add_workload_shading(ax)
    plot_latency(ax)
    fig.tight_layout()
    fig.savefig(os.path.join("results", "comparison_latency_p99.png"), dpi=150)
    fig.savefig(os.path.join("results", "comparison_latency_p99.pdf"), dpi=150)
    print(f"Saved: results/comparison_latency_p99.png/.pdf")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(10, 4))
    add_workload_shading(ax)
    plot_cpu_cores(ax)
    fig.tight_layout()
    fig.savefig(os.path.join("results", "comparison_cpu_cores.png"), dpi=150)
    fig.savefig(os.path.join("results", "comparison_cpu_cores.pdf"), dpi=150)
    print(f"Saved: results/comparison_cpu_cores.png/.pdf")
    plt.close(fig)

    # ── Combined report figure (4 rows, 1 column) ─────────────────────────────
    fig, (ax_rate, ax_lat, ax_cpu, ax_drop) = plt.subplots(4, 1, figsize=(11, 14), sharex=True)
    fig.suptitle("Autoscaler Comparison: Custom HPA vs Kubernetes HPA", fontsize=13, fontweight="bold")

    add_workload_shading(ax_rate)
    plot_request_rate(ax_rate)
    ax_rate.set_xlabel("")

    add_workload_shading(ax_lat)
    plot_latency(ax_lat)
    ax_lat.set_xlabel("")

    add_workload_shading(ax_cpu)
    plot_cpu_cores(ax_cpu)
    ax_cpu.set_xlabel("")

    add_workload_shading(ax_drop)
    plot_dropped_requests(ax_drop)

    fig.tight_layout()
    for ext in ("png", "pdf"):
        out = os.path.join("results", f"comparison_combined.{ext}")
        fig.savefig(out, dpi=150, bbox_inches="tight")
        print(f"Saved: {out}")
    plt.close(fig)

    generate_summary_table()


if __name__ == "__main__":
    main()
